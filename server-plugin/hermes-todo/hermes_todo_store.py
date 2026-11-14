"""Durable profile-scoped store for Hermes Todo.

Category, plan, workflow status, and deadline remain independent. SQLite is
the shared authority for Desktop, CLI, and optional JSON imports. Schema
migrations run automatically through schema v7.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
import math
from calendar import monthrange
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from hermes_constants import get_hermes_home as _get_hermes_home
except ImportError:  # Standalone source-tree execution and tests.
    _get_hermes_home = None

VALID_PLANS = frozenset({"now", "today", "later"})
VALID_STATUSES = frozenset({"open", "waiting", "blocked", "done"})
VALID_CATEGORIES = frozenset(
    {"today", "tomorrow", "this-week", "this-month", "soon"}
)
DEFAULT_CATEGORY = "today"
POSITION_STEP = 1024.0
POSITION_MAX = 1e15
POSITION_MIN = -1e15
VALID_EXECUTION_MODES = frozenset({"manual", "supervised", "autonomous"})
VALID_APPROVAL_STATES = frozenset({"not-required", "pending", "approved", "rejected"})
VALID_SESSION_STATES = frozenset({"active", "completed"})
VALID_RECURRENCE_RULES = frozenset({"daily", "weekdays", "weekly", "monthly"})
DEFAULT_ESTIMATE = 25
MAX_SOURCE_PAYLOAD_BYTES = 64 * 1024
MAX_EVENT_DATA_BYTES = 64 * 1024
MAX_ARTEFACTS = 20
MAX_ARTEFACT_LENGTH = 1000
MAX_SUBTASKS = 200
MAX_SUBTASK_TITLE = 500
SUBTASK_POSITION_STEP = 1024.0
SCHEMA_VERSION = 7
_UNSET = object()
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BoardError(ValueError):
    """A user-correctable board mutation error."""


class RevisionConflict(BoardError):
    """An optimistic write was based on an older board revision."""

    def __init__(self, expected_revision: int, current_revision: int) -> None:
        self.expected_revision = expected_revision
        self.current_revision = current_revision
        super().__init__(
            f"Revision conflict: expected {expected_revision}, current revision is {current_revision}"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _configured_hermes_home() -> Path:
    if _get_hermes_home is not None:
        return Path(_get_hermes_home()).expanduser()
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _hermes_root(home: Path) -> Path:
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def todo_profile_name() -> str:
    """Profile id from HERMES_TODO_PROFILE, or '' when that variable is unset."""
    requested = os.environ.get("HERMES_TODO_PROFILE", "").strip()
    if not requested:
        return ""
    if requested.casefold() == "default":
        return "default"
    return requested.lower()


def resolve_hermes_home() -> Path:
    """Return the Hermes home whose todo database this process should open."""
    active = _configured_hermes_home()
    name = todo_profile_name()
    if not name:
        return active
    root = _hermes_root(active)
    if name == "default":
        return root
    if not _PROFILE_ID_RE.fullmatch(name):
        requested = os.environ.get("HERMES_TODO_PROFILE", "").strip()
        raise BoardError(
            f"{requested!r} is not a Hermes profile name. "
            "Use letters, numbers, '-' or '_', starting with a letter or number, up to 64 characters."
        )
    profile_dir = root / "profiles" / name
    if not profile_dir.is_dir():
        raise BoardError(f"Hermes profile {name!r} was not found at {profile_dir}.")
    return profile_dir


def resolve_db_path() -> Path:
    """Return this profile's isolated Hermes Todo database path."""
    return resolve_hermes_home() / "hermes-todo" / "todo.sqlite3"


def _chmod_private_files(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            candidate.chmod(0o600)


def _create_v4_tasks(conn: sqlite3.Connection, table: str = "tasks") -> None:
    conn.execute(
        f"""
        CREATE TABLE {table} (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            plan TEXT NOT NULL CHECK (plan IN ('now', 'today', 'later')),
            status TEXT NOT NULL CHECK (status IN ('open', 'waiting', 'blocked', 'done')),
            category TEXT NOT NULL DEFAULT 'today'
                CHECK (category IN ('today', 'tomorrow', 'this-week', 'this-month', 'soon')),
            position REAL NOT NULL DEFAULT 0,
            estimate INTEGER NOT NULL CHECK (estimate BETWEEN 5 AND 480),
            due_date TEXT,
            due_at TEXT,
            due_timezone TEXT,
            due_language TEXT,
            source TEXT,
            external_id TEXT,
            project TEXT,
            priority INTEGER CHECK (priority IS NULL OR priority BETWEEN 1 AND 4),
            recurrence TEXT,
            source_updated_at TEXT,
            source_payload TEXT,
            brief TEXT,
            next_action TEXT,
            closure_condition TEXT,
            waiting_on TEXT,
            review_date TEXT,
            blocker TEXT,
            artefacts TEXT NOT NULL DEFAULT '[]',
            owner TEXT,
            execution_mode TEXT NOT NULL DEFAULT 'manual'
                CHECK (execution_mode IN ('manual', 'supervised', 'autonomous')),
            approval_state TEXT NOT NULL DEFAULT 'not-required'
                CHECK (approval_state IN ('not-required', 'pending', 'approved', 'rejected')),
            inbox INTEGER NOT NULL DEFAULT 0 CHECK (inbox IN (0, 1)),
            closure_note TEXT,
            closure_evidence TEXT NOT NULL DEFAULT '[]',
            recurrence_rule TEXT,
            recurrence_timezone TEXT,
            series_id TEXT,
            occurrence_id TEXT,
            occurrence_number INTEGER,
            session_id TEXT,
            session_state TEXT CHECK (session_state IS NULL OR session_state IN ('active', 'completed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            CHECK (due_date IS NULL OR due_at IS NULL),
            CHECK ((source IS NULL) = (external_id IS NULL))
        )
        """
    )


def _create_v4_support(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_v4_single_open_now
        ON tasks((1)) WHERE plan = 'now' AND status = 'open'""",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_v4_source_external
        ON tasks(source, external_id)
        WHERE source IS NOT NULL AND external_id IS NOT NULL""",
        """CREATE INDEX IF NOT EXISTS idx_tasks_v4_status_plan_due
        ON tasks(status, plan, due_date, due_at, created_at)""",
        """CREATE INDEX IF NOT EXISTS idx_tasks_v4_agenda
        ON tasks(inbox, review_date, updated_at)""",
        """CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            actor TEXT,
            source TEXT NOT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )""",
        """CREATE INDEX IF NOT EXISTS idx_task_events_task_created
        ON task_events(task_id, created_at, id)""",
        """CREATE TABLE IF NOT EXISTS subtasks (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
            position REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )""",
        """CREATE INDEX IF NOT EXISTS idx_subtasks_task_position
        ON subtasks(task_id, position, id)""",
    )
    for statement in statements:
        conn.execute(statement)
    try:
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_v4_occurrence
            ON tasks(occurrence_id) WHERE occurrence_id IS NOT NULL"""
        )
    except sqlite3.IntegrityError:
        # Older databases may already contain duplicate optional recurrence
        # metadata. Runtime identity checks protect new writes without making
        # an otherwise readable profile fail to open.
        pass


_V4_ADDITIONS = {
    "brief": "TEXT",
    "next_action": "TEXT",
    "closure_condition": "TEXT",
    "waiting_on": "TEXT",
    "review_date": "TEXT",
    "blocker": "TEXT",
    "artefacts": "TEXT NOT NULL DEFAULT '[]'",
    "owner": "TEXT",
    "execution_mode": "TEXT NOT NULL DEFAULT 'manual'",
    "approval_state": "TEXT NOT NULL DEFAULT 'not-required'",
    "inbox": "INTEGER NOT NULL DEFAULT 0",
    "closure_note": "TEXT",
    "closure_evidence": "TEXT NOT NULL DEFAULT '[]'",
    "recurrence_rule": "TEXT",
    "recurrence_timezone": "TEXT",
    "series_id": "TEXT",
    "occurrence_id": "TEXT",
    "occurrence_number": "INTEGER",
    "session_id": "TEXT",
    "session_state": "TEXT",
}


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    event_type: str,
    *,
    data: dict[str, Any] | None = None,
    actor: str | None = None,
    source: str = "store",
    created_at: str | None = None,
) -> None:
    clean_type = _clean_optional_text(event_type, "Event type", 100)
    clean_source = _clean_optional_text(source, "Event source", 100)
    if clean_type is None or clean_source is None:
        raise BoardError("Event type and source are required")
    clean_actor = _clean_optional_text(actor, "Event actor", 200)
    try:
        data_json = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise BoardError("Event data must be JSON-compatible") from exc
    if len(data_json.encode("utf-8")) > MAX_EVENT_DATA_BYTES:
        raise BoardError(f"Event data must be {MAX_EVENT_DATA_BYTES} UTF-8 bytes or fewer")
    conn.execute(
        """
        INSERT INTO task_events(task_id, event_type, actor, source, data_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, clean_type, clean_actor, clean_source, data_json, created_at or _utc_now()),
    )


def _migrate_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        _create_v4_support(conn)
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        if not table_exists:
            _create_v4_tasks(conn)
        else:
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "lane" in columns and "plan" not in columns:
                archive_exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks_v2_archive'"
                ).fetchone()
                if archive_exists:
                    raise RuntimeError("Existing tasks_v2_archive prevents a safe migration")
                conn.execute("ALTER TABLE tasks RENAME TO tasks_v2_archive")
                _create_v4_tasks(conn)
                rows = conn.execute(
                    """
                    SELECT id, title, lane, estimate, created_at, updated_at, completed_at
                    FROM tasks_v2_archive ORDER BY created_at, id
                    """
                ).fetchall()
                kept_now = False
                for row in rows:
                    lane = str(row["lane"])
                    plan = "now" if lane == "now" and not kept_now else "today"
                    status = {"waiting": "waiting", "blocked": "blocked", "done": "done"}.get(
                        lane, "open"
                    )
                    if plan == "now" and status == "open":
                        kept_now = True
                    completed_at = row["completed_at"] if status == "done" else None
                    conn.execute(
                        """
                        INSERT INTO tasks(
                            id, title, plan, status, estimate,
                            created_at, updated_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            row["id"], row["title"], plan, status, row["estimate"],
                            row["created_at"], row["updated_at"], completed_at,
                        ),
                    )
                legacy_count = int(
                    conn.execute("SELECT COUNT(*) FROM tasks_v2_archive").fetchone()[0]
                )
                migrated_count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
                if migrated_count != legacy_count:
                    raise RuntimeError("Hermes Todo migration row-count mismatch")
            elif {"plan", "status", "due_date", "due_at"}.issubset(columns):
                if "category" not in columns:
                    conn.execute(
                        "ALTER TABLE tasks ADD COLUMN category TEXT NOT NULL DEFAULT 'today'"
                        " CHECK (category IN ('today', 'tomorrow', 'this-week', 'this-month', 'soon'))"
                    )
                    conn.execute(
                        "UPDATE tasks SET category = 'soon' WHERE plan = 'later'"
                    )
                if "position" not in columns:
                    conn.execute("ALTER TABLE tasks ADD COLUMN position REAL NOT NULL DEFAULT 0")
                    conn.execute(
                        """
                        UPDATE tasks SET position = rowid
                        """
                    )
                for name, declaration in _V4_ADDITIONS.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")
            else:
                raise RuntimeError("Unversioned Hermes Todo schema cannot be migrated safely")

        _create_v4_support(conn)
        if "position" in {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }:
            conn.execute(
                "UPDATE tasks SET position = rowid WHERE position = 0"
            )
        rows = conn.execute(
            "SELECT id, plan, status, inbox, occurrence_id, created_at FROM tasks"
        ).fetchall()
        for row in rows:
            exists = conn.execute(
                "SELECT 1 FROM task_events WHERE task_id = ? AND event_type = 'task.created'",
                (row["id"],),
            ).fetchone()
            if not exists:
                _append_event(
                    conn,
                    str(row["id"]),
                    "task.created",
                    data={
                        "plan": row["plan"],
                        "status": row["status"],
                        "inbox": bool(row["inbox"]),
                        "occurrenceId": row["occurrence_id"],
                        "legacy": True,
                    },
                    source="migration",
                    created_at=str(row["created_at"]),
                )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _connect() -> sqlite3.Connection:
    path = resolve_db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    _chmod_private_files(path)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS board_meta (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO board_meta(key, value) VALUES ('revision', 0);
            """
        )
        _migrate_schema(conn)
    except BaseException:
        # A failed migration must not strand the handle: callers never receive
        # this connection, so nothing else can close it.
        conn.close()
        raise
    _chmod_private_files(path)
    return conn


def _clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise BoardError("Task title must be text")
    title = value.strip()
    if not title:
        raise BoardError("Task title is required")
    if len(title) > 500:
        raise BoardError("Task title must be 500 characters or fewer")
    return title


def _clean_choice(value: Any, choices: frozenset[str], field: str) -> str:
    if not isinstance(value, str):
        raise BoardError(f"{field.capitalize()} must be text")
    cleaned = value.strip().lower()
    if cleaned not in choices:
        raise BoardError(f"Unknown {field}: {cleaned or '(empty)'}")
    return cleaned


def _clean_estimate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoardError("Estimate must be a whole number of minutes")
    if value < 5 or value > 480:
        raise BoardError("Estimate must be between 5 and 480 minutes")
    return value


def _clean_position(value: Any, field: str = "Position") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BoardError(f"{field} must be a number")
    position = float(value)
    if not math.isfinite(position):
        raise BoardError(f"{field} must be a finite number")
    if position < POSITION_MIN or position > POSITION_MAX:
        raise BoardError(
            f"{field} must be between {POSITION_MIN:g} and {POSITION_MAX:g}"
        )
    return position


def _clean_due_date(value: Any, field: str = "Due date") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BoardError(f"{field} must be text in YYYY-MM-DD format")
    text = value.strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise BoardError(f"{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise BoardError(f"{field} must be YYYY-MM-DD")
    return text


def _clean_due_at(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BoardError("Due time must be an ISO datetime string")
    text = value.strip()
    if not text:
        return None
    if "T" not in text and " " not in text:
        raise BoardError("Timed due values need a datetime; use dueDate for all-day dates")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BoardError("Due time must be an ISO datetime") from exc
    return text


def _clean_optional_text(value: Any, field: str, max_length: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BoardError(f"{field} must be text")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_length:
        raise BoardError(f"{field} must be {max_length} characters or fewer")
    return text


def _clean_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise BoardError(f"{field} must be true or false")
    return value


def _clean_priority(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BoardError("Priority must be a whole number from 1 to 4")
    if value < 1 or value > 4:
        raise BoardError("Priority must be from 1 to 4")
    return value


def _clean_timestamp(value: Any, fallback: str, field: str = "Created time") -> str:
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        raise BoardError(f"{field} must be an ISO timestamp or integer milliseconds")
    if isinstance(value, int):
        try:
            return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        except (OSError, OverflowError, ValueError) as exc:
            raise BoardError(f"{field} is outside the supported range") from exc
    if not isinstance(value, str):
        raise BoardError(f"{field} must be an ISO timestamp or integer milliseconds")
    text = value.strip()
    if not text:
        return fallback
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BoardError(f"{field} must be an ISO timestamp or integer milliseconds") from exc
    return text


def _clean_source_payload(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise BoardError("Source payload must be valid JSON") from exc
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise BoardError("Source payload must be JSON-compatible") from exc
    if len(text.encode("utf-8")) > MAX_SOURCE_PAYLOAD_BYTES:
        raise BoardError(
            f"Source payload must be {MAX_SOURCE_PAYLOAD_BYTES} UTF-8 bytes or fewer"
        )
    return text


def _clean_string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BoardError(f"{field} must be a list of strings")
    if len(value) > MAX_ARTEFACTS:
        raise BoardError(f"{field} may contain at most {MAX_ARTEFACTS} items")
    cleaned: list[str] = []
    for item in value:
        text = _clean_optional_text(item, field, MAX_ARTEFACT_LENGTH)
        if text is not None and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def _clean_recurrence_rule(value: Any) -> str | None:
    text = _clean_optional_text(value, "Recurrence rule", 50)
    if text is None:
        return None
    rule = text.lower()
    if rule in VALID_RECURRENCE_RULES:
        return rule
    match = re.fullmatch(r"every:([1-9]\d{0,2})d", rule)
    if match and int(match.group(1)) <= 365:
        return rule
    raise BoardError(
        "Recurrence rule must be daily, weekdays, weekly, monthly, or every:<1-365>d"
    )


def _clean_timezone(value: Any, field: str = "Recurrence timezone") -> str | None:
    text = _clean_optional_text(value, field, 100)
    if text is None:
        return None
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BoardError(f"{field} must be a valid IANA timezone") from exc
    return text


def _legacy_to_dimensions(lane: Any) -> tuple[str, str]:
    if lane is None:
        value = "today"
    elif isinstance(lane, str):
        value = lane.strip().lower()
    else:
        raise BoardError("Lane must be text")
    if value == "now":
        return "now", "open"
    if value in {"today", "later"}:
        return value, "open"
    if value == "waiting":
        return "today", "waiting"
    if value == "blocked":
        return "today", "blocked"
    if value == "done":
        return "today", "done"
    raise BoardError(f"Unknown lane: {value or '(empty)'}")


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    plan = str(row["plan"])
    status = str(row["status"])
    return {
        "id": row["id"],
        "title": row["title"],
        "lane": status if status != "open" else plan,
        "plan": plan,
        "status": status,
        "category": str(row["category"]),
        "position": float(row["position"]),
        "estimate": row["estimate"],
        "dueDate": row["due_date"],
        "dueAt": row["due_at"],
        "dueTimezone": row["due_timezone"],
        "dueLanguage": row["due_language"],
        "source": row["source"],
        "externalId": row["external_id"],
        "project": row["project"],
        "priority": row["priority"],
        "recurrence": row["recurrence"],
        "sourceUpdatedAt": row["source_updated_at"],
        "brief": row["brief"],
        "nextAction": row["next_action"],
        "closureCondition": row["closure_condition"],
        "waitingOn": row["waiting_on"],
        "reviewDate": row["review_date"],
        "blocker": row["blocker"],
        "artefacts": _json_list(row["artefacts"]),
        "owner": row["owner"],
        "executionMode": row["execution_mode"],
        "approvalState": row["approval_state"],
        "inbox": bool(row["inbox"]),
        "closureNote": row["closure_note"],
        "closureEvidence": _json_list(row["closure_evidence"]),
        "recurrenceRule": row["recurrence_rule"],
        "recurrenceTimezone": row["recurrence_timezone"],
        "seriesId": row["series_id"],
        "occurrenceId": row["occurrence_id"],
        "occurrenceNumber": row["occurrence_number"],
        "sessionId": row["session_id"],
        "sessionState": row["session_state"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "completedAt": row["completed_at"],
    }


def _row_to_subtask(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "taskId": row["task_id"],
        "title": row["title"],
        "done": bool(row["done"]),
        "position": float(row["position"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "completedAt": row["completed_at"],
    }


def _subtasks_for(conn: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM subtasks WHERE task_id = ? ORDER BY position, id",
        (task_id,),
    ).fetchall()
    return [_row_to_subtask(row) for row in rows]


def _attach_subtasks(conn: sqlite3.Connection, tasks: list[dict[str, Any]]) -> None:
    if not tasks:
        return
    ids = [str(task["id"]) for task in tasks]
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM subtasks WHERE task_id IN ({placeholders}) ORDER BY position, id",
        ids,
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in ids}
    for row in rows:
        grouped[str(row["task_id"])].append(_row_to_subtask(row))
    for task in tasks:
        task["subtasks"] = grouped.get(str(task["id"]), [])
        task["subtaskCount"] = len(task["subtasks"])
        task["subtaskDoneCount"] = sum(1 for item in task["subtasks"] if item["done"])


def _revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM board_meta WHERE key = 'revision'").fetchone()
    return int(row["value"] if row else 0)


def _read_board(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT * FROM tasks
        ORDER BY
            CASE status WHEN 'open' THEN 0 WHEN 'waiting' THEN 1 WHEN 'blocked' THEN 2 ELSE 3 END,
            CASE plan WHEN 'now' THEN 0 WHEN 'today' THEN 1 ELSE 2 END,
            inbox DESC,
            position,
            created_at,
            id
        """
    ).fetchall()
    tasks = [_row_to_task(row) for row in rows]
    _attach_subtasks(conn, tasks)
    return {
        "version": SCHEMA_VERSION,
        "revision": _revision(conn),
        "tasks": tasks,
    }


def _bump_revision(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE board_meta SET value = value + 1 WHERE key = 'revision'")
    return _revision(conn)


def _check_expected_revision(conn: sqlite3.Connection, expected_revision: int | None) -> None:
    if expected_revision is None:
        return
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise BoardError("Expected revision must be a non-negative whole number")
    current = _revision(conn)
    if expected_revision != current:
        raise RevisionConflict(expected_revision, current)


def _demote_other_now(
    conn: sqlite3.Connection,
    task_id: str,
    now: str,
    *,
    actor: str | None,
    event_source: str,
) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'open' AND plan = 'now' AND id <> ?",
        (task_id,),
    ).fetchall()
    affected = [str(row["id"]) for row in rows]
    for affected_id in affected:
        conn.execute(
            "UPDATE tasks SET plan = 'today', updated_at = ? WHERE id = ?",
            (now, affected_id),
        )
        _append_event(
            conn,
            affected_id,
            "task.plan_changed",
            data={"from": "now", "to": "today", "reason": "single-open-now"},
            actor=actor,
            source=event_source,
            created_at=now,
        )
    return affected


def _task_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    affected_ids: Iterable[str] = (),
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    task = _row_to_task(row)
    _attach_subtasks(conn, [task])
    result: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "revision": _revision(conn),
        "task": task,
    }
    affected = []
    for affected_id in affected_ids:
        affected_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (affected_id,)).fetchone()
        if affected_row is not None:
            affected_task = _row_to_task(affected_row)
            _attach_subtasks(conn, [affected_task])
            affected.append(affected_task)
    if affected:
        result["affectedTasks"] = affected
    if extras:
        result.update(extras)
    return result


def _return_mutation(
    conn: sqlite3.Connection,
    result: dict[str, Any],
    return_board: bool,
) -> dict[str, Any]:
    if return_board:
        board = _read_board(conn)
        for key in (
            "affectedTasks",
            "generatedTask",
            "followUpTask",
            "imported",
            "skipped",
            "created",
            "deletedId",
            "deletedSubtaskId",
            "subtask",
        ):
            if key in result:
                board[key] = result[key]
        return board
    return result


def get_board() -> dict[str, Any]:
    with closing(_connect()) as conn:
        board = _read_board(conn)
    name = todo_profile_name()
    if name:
        board["todoProfile"] = name
    return board


def get_task(task_id: str) -> dict[str, Any]:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        task = _row_to_task(row)
        _attach_subtasks(conn, [task])
        return {
            "version": SCHEMA_VERSION,
            "revision": _revision(conn),
            "task": task,
        }


def _prepare_task_values(
    title: Any,
    *,
    estimate: Any = DEFAULT_ESTIMATE,
    plan: Any = None,
    status: Any = None,
    category: Any = None,
    position: Any = None,
    due_date: Any = None,
    due_at: Any = None,
    due_timezone: Any = None,
    due_language: Any = None,
    source: Any = None,
    external_id: Any = None,
    project: Any = None,
    priority: Any = None,
    recurrence: Any = None,
    source_updated_at: Any = None,
    source_payload: Any = None,
    lane: Any = None,
    task_id: Any = None,
    created_at: Any = None,
    updated_at: Any = None,
    completed_at: Any = None,
    brief: Any = None,
    next_action: Any = None,
    closure_condition: Any = None,
    waiting_on: Any = None,
    review_date: Any = None,
    blocker: Any = None,
    artefacts: Any = None,
    owner: Any = None,
    execution_mode: Any = "manual",
    approval_state: Any = "not-required",
    inbox: Any = False,
    closure_note: Any = None,
    closure_evidence: Any = None,
    recurrence_rule: Any = None,
    recurrence_timezone: Any = None,
    series_id: Any = None,
    occurrence_id: Any = None,
    occurrence_number: Any = None,
    session_id: Any = None,
    session_state: Any = None,
) -> dict[str, Any]:
    legacy_plan, legacy_status = _legacy_to_dimensions(lane)
    clean_plan = _clean_choice(plan if plan is not None else legacy_plan, VALID_PLANS, "plan")
    clean_status = _clean_choice(
        status if status is not None else legacy_status, VALID_STATUSES, "status"
    )
    clean_category = (
        _clean_choice(category, VALID_CATEGORIES, "category")
        if category is not None
        else DEFAULT_CATEGORY
    )
    clean_position = _clean_position(position) if position is not None else None
    clean_due_date = _clean_due_date(due_date)
    clean_due_at = _clean_due_at(due_at)
    if clean_due_date and clean_due_at:
        raise BoardError("A task cannot have both an all-day due date and a timed due value")
    clean_source = _clean_optional_text(source, "Source", 100)
    clean_external = _clean_optional_text(external_id, "External ID", 200)
    if bool(clean_source) != bool(clean_external):
        raise BoardError("Source and external ID must be provided together")
    rule = _clean_recurrence_rule(recurrence_…14832 tokens truncated…e:
                reason_codes.append("overdue")
            elif due == agenda_date:
                reason_codes.append("due_today")
            if task["status"] == "open" and task["plan"] == "today" and not task["inbox"]:
                reason_codes.append("ready_next")
            if task["status"] == "waiting" and task["reviewDate"]:
                try:
                    if date.fromisoformat(task["reviewDate"]) <= agenda_date:
                        reason_codes.append("waiting_review")
                except ValueError:
                    pass
            if task["status"] == "blocked" and task["blocker"]:
                reason_codes.append("blocked")
            try:
                updated = datetime.fromisoformat(str(task["updatedAt"]).replace("Z", "+00:00"))
                updated_date = updated.astimezone(zone).date() if updated.tzinfo else updated.date()
                if updated_date <= stale_before:
                    reason_codes.append("stale")
            except ValueError:
                pass
            if reason_codes:
                reasons = []
                for code in reason_codes:
                    label = _AGENDA_LABELS[code]
                    if code == "waiting_review":
                        label = f"Review waiting task on {task['reviewDate']}"
                    elif code == "blocked":
                        label = f"Blocked: {task['blocker']}"
                    reasons.append({"code": code, "label": label})
                candidates.append({"task": task, "reasons": reasons})
        candidates.sort(
            key=lambda item: (
                min(_AGENDA_RANK[reason["code"]] for reason in item["reasons"]),
                item["task"]["dueDate"] or item["task"]["dueAt"] or "9999",
                item["task"]["priority"] or 9,
                item["task"]["createdAt"],
            )
        )
        return {
            "version": SCHEMA_VERSION,
            "revision": _revision(conn),
            "generatedAt": _utc_now(),
            "date": agenda_date.isoformat(),
            "timezone": zone_name,
            "staleAfterDays": stale_days,
            "total": len(candidates),
            "truncated": len(candidates) > limit,
            "items": candidates[:limit],
        }


def link_task_session(
    task_id: str,
    session_id: str,
    *,
    start_now: bool = True,
    replace_active: bool = False,
    expected_revision: int | None = None,
    actor: str | None = None,
    event_source: str = "store",
    return_board: bool = False,
) -> dict[str, Any]:
    clean_session_id = _clean_optional_text(session_id, "Session ID", 300)
    if clean_session_id is None:
        raise BoardError("Session ID is required")
    start_now = _clean_bool(start_now, "Start now")
    replace_active = _clean_bool(replace_active, "Replace active")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_expected_revision(conn, expected_revision)
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if existing is None:
            raise KeyError(task_id)
        replacing = False
        if existing["session_state"] == "active":
            if existing["session_id"] == clean_session_id:
                result = _task_result(conn, task_id)
                conn.commit()
                return _return_mutation(conn, result, return_board)
            if not replace_active:
                raise BoardError("Task already has an active linked session")
            replacing = True
        elif existing["status"] != "open":
            raise BoardError("Reopen and clear waiting or blocking context before starting work")
        now = _utc_now()
        affected: list[str] = []
        plan = existing["plan"]
        if start_now:
            plan = "now"
            affected = _demote_other_now(
                conn, task_id, now, actor=actor, event_source=event_source
            )
        conn.execute(
            """
            UPDATE tasks SET session_id = ?, session_state = 'active', plan = ?, inbox = 0,
                updated_at = ? WHERE id = ?
            """,
            (clean_session_id, plan, now, task_id),
        )
        if start_now and existing["plan"] != "now":
            _append_event(
                conn, task_id, "task.plan_changed",
                data={"from": existing["plan"], "to": "now", "reason": "session-started"},
                actor=actor, source=event_source, created_at=now,
            )
        if replacing:
            _append_event(
                conn, task_id, "session.replaced",
                data={"from": existing["session_id"], "to": clean_session_id},
                actor=actor, source=event_source, created_at=now,
            )
        _append_event(
            conn, task_id, "session.started", data={"sessionId": clean_session_id},
            actor=actor, source=event_source, created_at=now,
        )
        _bump_revision(conn)
        result = _task_result(conn, task_id, affected_ids=affected)
        conn.commit()
        return _return_mutation(conn, result, return_board)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_task_session(
    task_id: str,
    *,
    expected_revision: int | None = None,
    actor: str | None = None,
    event_source: str = "store",
    return_board: bool = False,
) -> dict[str, Any]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_expected_revision(conn, expected_revision)
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if existing is None:
            raise KeyError(task_id)
        if existing["session_state"] != "active":
            result = _task_result(conn, task_id)
            conn.commit()
            return _return_mutation(conn, result, return_board)
        now = _utc_now()
        conn.execute(
            "UPDATE tasks SET session_state = 'completed', updated_at = ? WHERE id = ?",
            (now, task_id),
        )
        _append_event(
            conn, task_id, "session.completed", data={"sessionId": existing["session_id"]},
            actor=actor, source=event_source, created_at=now,
        )
        _bump_revision(conn)
        result = _task_result(conn, task_id)
        conn.commit()
        return _return_mutation(conn, result, return_board)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def generate_next_occurrence(
    task_id: str,
    *,
    expected_revision: int | None = None,
    actor: str | None = None,
    event_source: str = "store",
    return_board: bool = False,
) -> dict[str, Any]:
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_expected_revision(conn, expected_revision)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["status"] != "done":
            raise BoardError("Complete the current occurrence before generating the next one")
        generated, created = _generate_next_occurrence(
            conn, row, actor=actor, event_source=event_source, now=_utc_now()
        )
        if created:
            _bump_revision(conn)
        result = _task_result(
            conn,
            task_id,
            extras={"generatedTask": generated, "created": created},
        )
        conn.commit()
        return _return_mutation(conn, result, return_board)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_with_follow_up(
    task_id: str,
    follow_up: dict[str, Any],
    *,
    closure_note: Any = _UNSET,
    closure_evidence: Any = _UNSET,
    expected_revision: int | None = None,
    actor: str | None = None,
    event_source: str = "store",
    return_board: bool = False,
) -> dict[str, Any]:
    if not isinstance(follow_up, dict):
        raise BoardError("Follow-up must be a task object")
    follow_fields = dict(follow_up)
    title = follow_fields.pop("title", None)
    follow_values = _prepare_task_values(title, **{_ALIASES.get(k, k): v for k, v in follow_fields.items()})
    note_was_provided = closure_note is not _UNSET
    evidence_was_provided = closure_evidence is not _UNSET
    clean_note = (
        _clean_optional_text(closure_note, "Closure note", 4000)
        if note_was_provided
        else None
    )
    clean_evidence = (
        json.dumps(
            _clean_string_list(closure_evidence, "Closure evidence"), ensure_ascii=False
        )
        if evidence_was_provided
        else None
    )
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_expected_revision(conn, expected_revision)
        existing = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if existing is None:
            raise KeyError(task_id)
        if existing["status"] == "done":
            raise BoardError("Task is already complete")
        now = _utc_now()
        final_note = clean_note if note_was_provided else existing["closure_note"]
        final_evidence = (
            clean_evidence if evidence_was_provided else (existing["closure_evidence"] or "[]")
        )
        session_state = "completed" if existing["session_state"] == "active" else existing["session_state"]
        conn.execute(
            """
            UPDATE tasks SET status = 'done', completed_at = ?, updated_at = ?,
                closure_note = ?, closure_evidence = ?, session_state = ? WHERE id = ?
            """,
            (now, now, final_note, final_evidence, session_state, task_id),
        )
        _append_event(
            conn, task_id, "task.status_changed",
            data={"from": existing["status"], "to": "done"}, actor=actor,
            source=event_source, created_at=now,
        )
        _append_event(
            conn, task_id, "task.completed",
            data={"closureNote": final_note, "closureEvidence": json.loads(final_evidence)},
            actor=actor, source=event_source, created_at=now,
        )
        if existing["session_state"] == "active":
            _append_event(
                conn, task_id, "session.completed",
                data={"sessionId": existing["session_id"], "reason": "task-completed"},
                actor=actor, source=event_source, created_at=now,
            )
        current = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        generated, _ = _generate_next_occurrence(
            conn, current, actor=actor, event_source=event_source, now=now
        )
        if follow_values.get("position") is None:
            follow_values["position"] = _next_position_on_conn(conn)
        affected: list[str] = []
        if follow_values["status"] == "open" and follow_values["plan"] == "now":
            affected = _demote_other_now(
                conn, follow_values["id"], now, actor=actor, event_source=event_source
            )
        _insert_values(conn, follow_values)
        _append_event(
            conn, follow_values["id"], "task.created",
            data={"plan": follow_values["plan"], "status": follow_values["status"], "inbox": bool(follow_values["inbox"]), "followUpTo": task_id},
            actor=actor, source=event_source, created_at=follow_values["created_at"],
        )
        _bump_revision(conn)
        follow_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (follow_values["id"],)).fetchone()
        result = _task_result(
            conn,
            task_id,
            affected_ids=affected,
            extras={
                "followUpTask": _row_to_task(follow_row),
                "generatedTask": generated,
            },
        )
        conn.commit()
        return _return_mutation(conn, result, return_board)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise BoardError("Complete-and-follow-up violates a board invariant") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_IMPORT_KEYS = {
    "id", "title", "lane", "plan", "status", "category", "position", "estimate", "dueDate", "dueAt",
    "dueTimezone", "dueLanguage", "source", "externalId", "project", "priority",
    "recurrence", "sourceUpdatedAt", "sourcePayload", "createdAt", "brief",
    "nextAction", "closureCondition", "waitingOn", "reviewDate", "blocker", "artefacts",
    "owner", "executionMode", "approvalState", "inbox", "closureNote", "closureEvidence",
    "recurrenceRule", "recurrenceTimezone", "seriesId", "occurrenceId", "occurrenceNumber",
    "sessionId", "sessionState", "updatedAt", "completedAt", "subtasks",
}


def _validate_import_occurrence_identity(raw: dict[str, Any]) -> None:
    identity_keys = {"seriesId", "occurrenceId", "occurrenceNumber"}
    supplied = identity_keys.intersection(raw)
    if supplied and supplied != identity_keys:
        raise BoardError(
            "Imported seriesId, occurrenceId, and occurrenceNumber must be provided together"
        )
    if not supplied:
        return
    series_id = _clean_optional_text(raw.get("seriesId"), "Series ID", 200)
    occurrence_id = _clean_optional_text(raw.get("occurrenceId"), "Occurrence ID", 240)
    occurrence_number = raw.get("occurrenceNumber")
    if (
        series_id is None
        or occurrence_id is None
        or isinstance(occurrence_number, bool)
        or not isinstance(occurrence_number, int)
        or occurrence_number < 1
    ):
        raise BoardError(
            "Imported seriesId, occurrenceId, and occurrenceNumber must form a complete identity"
        )
    if occurrence_id != f"{series_id}:{occurrence_number}":
        raise BoardError("Imported occurrenceId must match <seriesId>:<occurrenceNumber>")


def _import_source_payload(raw: dict[str, Any]) -> Any:
    unknown = {key: value for key, value in raw.items() if key not in _IMPORT_KEYS}
    payload = raw.get("sourcePayload")
    if not unknown:
        return payload
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"value": payload}
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    payload = dict(payload)
    if "legacyFields" not in payload:
        payload["legacyFields"] = unknown
    elif isinstance(payload["legacyFields"], dict):
        merged_legacy = dict(unknown)
        merged_legacy.update(payload["legacyFields"])
        payload["legacyFields"] = merged_legacy
    else:
        existing_imported = payload.get("importedTopLevelFields")
        merged_imported = dict(unknown)
        if isinstance(existing_imported, dict):
            merged_imported.update(existing_imported)
        payload["importedTopLevelFields"] = merged_imported
    return payload


def import_tasks(
    tasks: Iterable[dict[str, Any]],
    *,
    expected_revision: int | None = None,
    actor: str | None = None,
    event_source: str = "import",
    return_board: bool = True,
) -> dict[str, Any]:
    """Atomically insert missing tasks without changing previously imported rows."""
    raw_tasks = list(tasks)
    inserted = 0
    skipped = 0
    inserted_ids: list[str] = []
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _check_expected_revision(conn, expected_revision)
        has_open_now = conn.execute(
            "SELECT 1 FROM tasks WHERE plan = 'now' AND status = 'open'"
        ).fetchone() is not None
        for index, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise BoardError(f"Import task {index + 1} must be an object")
            _validate_import_occurrence_identity(raw)
            task_id = _clean_optional_text(raw.get("id"), "Task ID", 200) or uuid.uuid4().hex
            source = _clean_optional_text(raw.get("source"), "Source", 100)
            external_id = _clean_optional_text(raw.get("externalId"), "External ID", 200)
            if bool(source) != bool(external_id):
                raise BoardError("Source and external ID must be provided together")
            exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not exists and source and external_id:
                exists = conn.execute(
                    "SELECT 1 FROM tasks WHERE source = ? AND external_id = ?",
                    (source, external_id),
                ).fetchone()
            if exists:
                skipped += 1
                continue
            values = _prepare_task_values(
                raw.get("title"),
                task_id=task_id,
                lane=raw.get("lane"),
                plan=raw.get("plan"),
                status=raw.get("status"),
                category=raw.get("category"),
                position=raw.get("position"),
                estimate=raw.get("estimate", DEFAULT_ESTIMATE),
                due_date=raw.get("dueDate"),
                due_at=raw.get("dueAt"),
                due_timezone=raw.get("dueTimezone"),
                due_language=raw.get("dueLanguage"),
                source=source,
                external_id=external_id,
                project=raw.get("project"),
                priority=raw.get("priority"),
                recurrence=raw.get("recurrence"),
                source_updated_at=raw.get("sourceUpdatedAt"),
                source_payload=_import_source_payload(raw),
                created_at=raw.get("createdAt"),
                updated_at=raw.get("updatedAt"),
                completed_at=raw.get("completedAt"),
                brief=raw.get("brief"),
                next_action=raw.get("nextAction"),
                closure_condition=raw.get("closureCondition"),
                waiting_on=raw.get("waitingOn"),
                review_date=raw.get("reviewDate"),
                blocker=raw.get("blocker"),
                artefacts=raw.get("artefacts"),
                owner=raw.get("owner"),
                execution_mode=raw.get("executionMode", "manual"),
                approval_state=raw.get("approvalState", "not-required"),
                inbox=raw.get("inbox", False),
                closure_note=raw.get("closureNote"),
                closure_evidence=raw.get("closureEvidence"),
                recurrence_rule=raw.get("recurrenceRule"),
                recurrence_timezone=raw.get("recurrenceTimezone"),
                series_id=raw.get("seriesId"),
                occurrence_id=raw.get("occurrenceId"),
                occurrence_number=raw.get("occurrenceNumber"),
                session_id=raw.get("sessionId"),
                session_state=raw.get("sessionState"),
            )
            if values["occurrence_id"]:
                duplicate_occurrence = conn.execute(
                    "SELECT id FROM tasks WHERE occurrence_id = ?",
                    (values["occurrence_id"],),
                ).fetchone()
                if duplicate_occurrence is not None:
                    raise BoardError(
                        f"Occurrence identity already exists: {values['occurrence_id']}"
                    )
            if values["plan"] == "now" and values["status"] == "open" and has_open_now:
                values["plan"] = "today"
            if values["position"] is None:
                values["position"] = _next_position_on_conn(conn)
            _insert_values(conn, values)
            _import_subtasks(conn, values["id"], raw.get("subtasks"), values["created_at"])
            _append_event(
                conn, values["id"], "task.created",
                data={"plan": values["plan"], "status": values["status"], "inbox": bool(values["inbox"]), "imported": True},
                actor=actor, source=event_source, created_at=values["created_at"],
            )
            inserted += 1
            inserted_ids.append(values["id"])
            if values["plan"] == "now" and values["status"] == "open":
                has_open_now = True
        if inserted:
            _bump_revision(conn)
        result = {
            "version": SCHEMA_VERSION,
            "revision": _revision(conn),
            "imported": inserted,
            "skipped": skipped,
            "tasks": [
                _row_to_task(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
                for task_id in inserted_ids
            ],
        }
        _attach_subtasks(conn, result["tasks"])
        conn.commit()
        return _return_mutation(conn, result, return_board)
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise BoardError("Import violates a board invariant") from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

"""Personal DingTalk tasks and transactional reminder outbox (stdlib only)."""
from __future__ import annotations
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo
try:
    from . import hermes_todo_store as core
except ImportError:
    import hermes_todo_store as core

UTC = dt.timezone.utc

def home():
    return Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes'))

def settings():
    return json.loads((home() / 'hermes-todo/personal-settings.json').read_text())

def enabled():
    try:
        return settings().get('enabled', False)
    except (OSError, ValueError):
        return False

def stamp(value):
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('时间必须包含时区，例如 2026-11-15T09:00:00+08:00')
    return parsed.astimezone(UTC).isoformat()

def now():
    return dt.datetime.now(UTC).isoformat()

def connect():
    conn = core._connect()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS personal_items(
      number INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT UNIQUE NOT NULL,
      reminder_at TEXT, lead_minutes INTEGER, generation INTEGER NOT NULL DEFAULT 1,
      cancelled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS personal_requests(
      request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS personal_deliveries(
      id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, generation INTEGER NOT NULL,
      scheduled_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER DEFAULT 0,
      next_attempt TEXT, lease_until TEXT, receipt TEXT, error TEXT,
      UNIQUE(task_id,generation));
    CREATE TABLE IF NOT EXISTS personal_state(key TEXT PRIMARY KEY,value TEXT);
    ''')
    return conn

@contextlib.contextmanager
def locked():
    path = core.resolve_db_path().parent
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path / 'personal.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield

def enrich(conn, task):
    task = dict(task)
    row = conn.execute('SELECT * FROM personal_items WHERE task_id=?', (task['id'],)).fetchone()
    if row:
        task.update(taskNo=f"T{row['number']:06d}", reminderAt=row['reminder_at'],
                    reminderBeforeMinutes=row['lead_minutes'], cancelled=bool(row['cancelled']))
        if row['cancelled']:
            task['status'] = 'cancelled'
        delivery = conn.execute('SELECT state,attempts,error FROM personal_deliveries WHERE task_id=? AND generation=?',
                                (task['id'], row['generation'])).fetchone()
        task['delivery'] = dict(delivery) if delivery else None
    return task

def find(conn, identifier):
    row = conn.execute('SELECT task_id FROM personal_items WHERE task_id=? OR printf(\'T%06d\',number)=?',
                       (identifier, str(identifier).upper())).fetchone()
    if not row:
        raise ValueError('找不到个人待办；请先列出事项取得编号')
    return row['task_id']

def reminder(args, task, old=None):
    lead = old['lead_minutes'] if old else None
    at = old['reminder_at'] if old else None
    if args.get('no_reminder'):
        return None, None
    if 'remind_at' in args:
        return stamp(args['remind_at']), (old['lead_minutes'] if args.get('action') == 'snooze' and old else None)
    if 'remind_before_minutes' in args:
        lead = args['remind_before_minutes']
        if not isinstance(lead, int) or isinstance(lead, bool) or not 0 <= lead <= 525600:
            raise ValueError('提前提醒分钟数必须在 0 到 525600 之间')
    if lead is not None and (old is None or any(key in args for key in ('due_at','due_date','remind_before_minutes'))):
        if not task.get('dueAt'):
            raise ValueError('提前提醒需要带时间和时区的截止时间')
        at = (dt.datetime.fromisoformat(stamp(task['dueAt'])) - dt.timedelta(minutes=lead)).isoformat()
    elif old is None and task.get('dueAt'):
        lead, at = 0, stamp(task['dueAt'])
    return at, lead

def apply(args, request_id=None):
    action = args.get('action', 'list')
    with locked(), contextlib.closing(connect()) as conn:
        if action == 'list':
            tasks = core.get_board()['tasks']
            items = [enrich(conn, t) for t in tasks if conn.execute('SELECT 1 FROM personal_items WHERE task_id=?',(t['id'],)).fetchone()]
            if not args.get('include_closed'):
                items = [t for t in items if t['status'] not in ('done','cancelled')]
            return {'tasks':items, 'timezone':settings().get('timezone','Asia/Shanghai')}
        if action == 'status':
            return {'deliveries':[dict(r) for r in conn.execute('SELECT state,count(*) AS count FROM personal_deliveries GROUP BY state')],
                    'worker':dict(conn.execute('SELECT key,value FROM personal_state'))}
        identifier = args.get('task_id')
        task_id = find(conn, identifier) if action != 'create' else None
        if action == 'get':
            return {'task':enrich(conn, core.get_task(task_id)['task'])}
        if action == 'history':
            return {'history':core.get_history(task_id), 'deliveries':[dict(r) for r in conn.execute('SELECT * FROM personal_deliveries WHERE task_id=?',(task_id,))]}
        fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if request_id:
            previous = conn.execute('SELECT * FROM personal_requests WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('本条消息中的操作编号已用于不同参数；请先读取结果，勿重复新增')
                return json.loads(previous['result'])
        old = conn.execute('SELECT * FROM personal_items WHERE task_id=?',(task_id,)).fetchone() if task_id else None
        fields = {k:args[k] for k in ('title','due_at','due_date','priority','recurrence_rule','brief') if k in args}
        if 'due_at' in fields:
            fields['due_at'] = stamp(fields['due_at'])
            fields['due_date'] = None
            fields['due_timezone'] = settings().get('timezone','Asia/Shanghai')
        if 'due_date' in fields and fields['due_date']:
            fields['due_at'] = None
        if action == 'complete': fields['status'] = 'done'
        elif action == 'cancel':
            fields.update(status='done', recurrence_rule=None, closure_note='已取消（非完成）')
        elif action == 'reopen': fields['status'] = 'open'
        elif action == 'snooze':
            if not args.get('remind_at'): raise ValueError('延后需要指定新的提醒时间')
        elif action not in ('create','update'): raise ValueError('未知操作')
        def on_write(db, result):
            task = result['task']
            at, lead = reminder(args, task, old)
            cancelled = action == 'cancel' or bool(old and old['cancelled'] and action != 'reopen')
            if action in ('complete','cancel','reopen'):
                at = None  # Reopening never replays a previous reminder.
            if task['status'] == 'done' and at:
                at = None
            if at and (old is None or at != old['reminder_at']) and dt.datetime.fromisoformat(at) <= dt.datetime.now(UTC):
                raise ValueError('提醒时间必须在未来；请明确一个新的提醒时间')
            generation = old['generation'] if old else 1
            if old and (at != old['reminder_at'] or cancelled != bool(old['cancelled'])):
                generation += 1
                db.execute("UPDATE personal_deliveries SET state='cancelled' WHERE task_id=? AND state IN ('pending','retry')",(task['id'],))
            db.execute('''INSERT INTO personal_items(task_id,reminder_at,lead_minutes,generation,cancelled) VALUES(?,?,?,?,?)
              ON CONFLICT(task_id) DO UPDATE SET reminder_at=excluded.reminder_at,lead_minutes=excluded.lead_minutes,
              generation=excluded.generation,cancelled=excluded.cancelled''',(task['id'],at,lead,generation,int(cancelled)))
            generated = result.get('generatedTask')
            if generated:
                next_at, next_lead = reminder({'remind_before_minutes':lead}, generated) if lead is not None else (None,None)
                db.execute('INSERT OR IGNORE INTO personal_items(task_id,reminder_at,lead_minutes) VALUES(?,?,?)',
                           (generated['id'],next_at,next_lead))
                result['generatedTask'] = enrich(db, generated)
            result['task'] = enrich(db, task)
            if request_id:
                db.execute('INSERT INTO personal_requests VALUES(?,?,?)',(request_id,fingerprint,json.dumps(result,ensure_ascii=False)))
        if action == 'create':
            title = fields.pop('title', None)
            if not title: raise ValueError('新增事项需要标题')
            return core.create_task(title, return_board=False, _on_write=on_write, event_source='personal-todo', **fields)
        return core.update_task(task_id, fields, return_board=False, _on_write=on_write, event_source='personal-todo')

PROPERTIES = {
 'action':{'type':'string','enum':['create','list','get','update','complete','cancel','reopen','snooze','status','history']},
 'task_id':{'type':'string','description':'事项编号，例如 T000001'},
 'operation_key':{'type':'string','description':'同一条消息中每个独立操作的稳定编号，例如 1、2。重试沿用。'},
 'title':{'type':'string'}, 'brief':{'type':'string'},
 'due_at':{'type':['string','null'],'description':'ISO8601截止时间，必须有时区'},
 'due_date':{'type':['string','null'],'description':'只有日期，无指定时间则不自动提醒'},
 'remind_at':{'type':['string','null'],'description':'ISO8601提醒时间，必须有时区'},
 'remind_before_minutes':{'type':'integer','minimum':0},
 'no_reminder':{'type':'boolean'}, 'include_closed':{'type':'boolean'},
 'priority':{'type':['integer','null'],'minimum':1,'maximum':4,'description':'1最高，4最低'},
 'recurrence_rule':{'type':['string','null'],'description':'daily / weekdays / weekly / monthly / every:Nd；完成后生成下一期'},
}
SCHEMA = {'name':'personal_todo','description':'个人待办及钉钉定时提醒。唯一事实来源；操作后按返回结果确认。时间默认中国时区。重复事项在完成后生成下一期。',
          'parameters':{'type':'object','properties':PROPERTIES,'required':['action'],'additionalProperties':False}}

def tool(args, **kwargs):
    try:
        from gateway.session_context import get_session_env
        cfg = settings()
        env = {key:get_session_env(key) for key in ('HERMES_SESSION_PLATFORM','HERMES_SESSION_KEY','HERMES_SESSION_USER_ID','HERMES_SESSION_MESSAGE_ID')}
        allowed = os.environ.get('DINGTALK_ALLOWED_USERS','')
        if (not cfg.get('enabled') or env.get('HERMES_SESSION_PLATFORM') != 'dingtalk'
            or ':dm:' not in env.get('HERMES_SESSION_KEY','')
            or not env.get('HERMES_SESSION_USER_ID') or not env.get('HERMES_SESSION_MESSAGE_ID')
            or allowed.strip() != cfg['owner_staff_id']):
            return json.dumps({'ok':False,'error':'仅允许已授权的钉钉私聊使用个人待办'},ensure_ascii=False)
        args = dict(args)
        operation = args.pop('operation_key','1')
        key = hashlib.sha256((env['HERMES_SESSION_USER_ID']+'|'+env['HERMES_SESSION_MESSAGE_ID']+'|'+str(operation)).encode()).hexdigest()
        return json.dumps({'ok':True, **apply(args,key)},ensure_ascii=False)
    except (ValueError, KeyError, OSError, core.BoardError) as exc:
        return json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False)

def main():
    import argparse
    parser = argparse.ArgumentParser(description='个人待办本地管理；JSON参数不接受接收人或密钥')
    parser.add_argument('request',help='JSON object')
    args = parser.parse_args()
    print(json.dumps(apply(json.loads(args.request)),ensure_ascii=False,indent=2))

if __name__ == '__main__': main()

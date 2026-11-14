# Personal DingTalk extension (fork v0.3.0)

Built on DanBennettUK/hermes-todo upstream commit 14c31fce0045f1ce25a9257734e8434c008fd570 (v0.2.1). Upstream license and core remain intact. Changes live on `dingtalk-personal-todo` in https://github.com/gtang94/hermes-todo .

Features: private-chat CRUD, per-task reminders, snooze, cancellation, reopen, completion-driven recurrence, transactional request deduplication and durable notification outbox. No daily digest or native DingTalk Todo integration. No extra LLM key is required: chat uses existing Hermes model and scans use no LLM.

## Installation

1. Back up Hermes config, environment, cron state and any existing todo database using SQLite Backup API.
2. Copy `server-plugin/hermes-todo` to `$HERMES_HOME/plugins/hermes-todo`; copy `skill` to `$HERMES_HOME/skills/personal-todo`; copy `todo_tick.py` to `$HERMES_HOME/scripts/todo_tick.py`. Runtime owner must own these paths.
3. Add `hermes-todo` to config `plugins.enabled` and `personal-todo` to `platform_toolsets.dingtalk` (preserve the platform default `hermes-dingtalk` if previously absent), preserving existing entries.
4. Write private mode-0600 `$HERMES_HOME/hermes-todo/personal-settings.json`:

```json
{"enabled":true,"owner_staff_id":"YOUR_STAFF_ID","timezone":"Asia/Shanghai","max_late_seconds":86400,"max_attempts":3,"digest_enabled":false}
```

Restrict `DINGTALK_ALLOWED_USERS` to exactly that owner (also restrict any `platforms.dingtalk.extra.allowed_users` override). Configure existing DingTalk application environment credentials on the server. This deployment reuses `/opt/hermes/integrations/dingtalk_private_notify.py` with `send_private_markdown(title,text,user_ids=[owner])`. It requires receipts containing `processQueryKey`. The adapter is specific to the existing 114 Hermes integration, not a public generic webhook.

5. As the Hermes runtime owner create one job:

```
hermes cron create '* * * * *' --name personal-todo-notifier --script todo_tick.py --no-agent --deliver local
```

6. Reload gateway. Verify plugin tools, context-based authorization and real scheduled receipt. Keep Compose unchanged.

## Maintenance

```
python $HERMES_HOME/plugins/hermes-todo/personal_todo.py '{"action":"status"}'
python $HERMES_HOME/plugins/hermes-todo/personal_todo.py '{"action":"list","include_closed":true}'
```

SQLite: `$HERMES_HOME/hermes-todo/todo.sqlite3`; native schema version stays 7; extension uses separate tables. Daily consistent snapshots in adjacent `backups/` retain 14 days; these are on the same server, not disaster recovery copies. Protect and export them through your usual server backup process.

Agent tools allow only whitelisted DingTalk private conversations, using trusted Hermes session context. Local operator scripts run with filesystem administrator privileges; this is not a security sandbox against other tools running as that same account. Original native todo_board is disabled in personal mode to prevent accidental bypass. Use personal tools for managed tasks; native Desktop/CLI writes do not carry personal request dedup/reminder semantics.

Only explicitly timed deadlines auto-remind at deadline; date-only tasks do not infer a time. Independent `remind_at` is one occurrence; relative lead minutes carry to the next completion-generated occurrence. Snooze changes notification time, not deadline. Cancel stores native status done with a cancellation note and personal cancelled flag; it removes recurrence and does not generate a successor. Reopen never replays a prior reminder.

Minute scans catch up at most 24 hours late (10 deliveries per scan). Older notifications become expired. Failed network calls retry at most 3 times with backoff. A 5-minute lease recovers crashed workers. HTTP timeout/crash after server acceptance can produce a rare duplicate; stable notification ID is included. A cancellation concurrent with an already-started HTTP request may still allow that in-flight notice. `accepted` means DingTalk accepted the request, not mobile delivery/read confirmation. There is no always-on independent failure-alert channel: inspect `status/history` and Hermes cron health.

Rollback: remove only the personal-todo-notifier cron job, restore saved config/environment, reload gateway, retain the todo database for recovery. Do not restore a stale whole-server Compose or other workloads.

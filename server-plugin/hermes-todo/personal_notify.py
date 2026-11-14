"""Minute scanner with durable leases, bounded retries and daily SQLite backup."""
from __future__ import annotations
import contextlib
import datetime as dt
import importlib.util
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo
try:
    from . import personal_todo as p
except ImportError:
    import personal_todo as p

def send(title, text, owner):
    path = '/opt/hermes/integrations/dingtalk_private_notify.py'
    spec = importlib.util.spec_from_file_location('personal_dingtalk_transport', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    responses = module.send_private_markdown(title, text, user_ids=[owner])
    if not responses or not isinstance(responses,list) or any(not isinstance(r,dict) or not r.get('processQueryKey') or r.get('invalidStaffIdList') or r.get('flowControlledStaffIdList') for r in responses):
        raise RuntimeError('DingTalk receipt missing or rejected')
    return json.dumps([r['processQueryKey'] for r in responses])

def tick(sender=send, clock=None):
    cfg = p.settings()
    if not cfg.get('enabled'): return {'disabled':True}
    current = clock or dt.datetime.now(p.UTC)
    instant = current.isoformat()
    expired = (current-dt.timedelta(seconds=cfg.get('max_late_seconds',86400))).isoformat()
    with p.locked(), contextlib.closing(p.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE personal_deliveries SET state='retry' WHERE state='sending' AND lease_until<?",(instant,))
        db.execute('''INSERT OR IGNORE INTO personal_deliveries(task_id,generation,scheduled_at)
          SELECT p.task_id,p.generation,p.reminder_at FROM personal_items p JOIN tasks t ON t.id=p.task_id
          WHERE p.cancelled=0 AND t.status!='done' AND p.reminder_at IS NOT NULL AND p.reminder_at<=?''',(instant,))
        db.execute("UPDATE personal_deliveries SET state='expired',error='超过补发时限' WHERE state IN ('pending','retry') AND scheduled_at<?",(expired,))
        db.execute("INSERT OR REPLACE INTO personal_state VALUES('last_scan',?)",(instant,))
        db.commit()
    results = []
    for _ in range(10):
        with p.locked(), contextlib.closing(p.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM personal_deliveries WHERE state IN ('pending','retry') AND (next_attempt IS NULL OR next_attempt<=?) ORDER BY scheduled_at LIMIT 1",(instant,)).fetchone()
            if not row:
                db.commit()
                break
            meta = db.execute('SELECT p.*,t.status,t.title,t.due_at FROM personal_items p JOIN tasks t ON p.task_id=t.id WHERE p.task_id=?',(row['task_id'],)).fetchone()
            if not meta or meta['cancelled'] or meta['status']=='done' or meta['generation']!=row['generation'] or not meta['reminder_at']:
                db.execute("UPDATE personal_deliveries SET state='cancelled' WHERE id=?",(row['id'],))
                db.commit()
                continue
            # Lease exceeds the helper's bounded HTTP timeouts. No DB lock during network calls.
            db.execute("UPDATE personal_deliveries SET state='sending',attempts=attempts+1,lease_until=? WHERE id=?",
                       ((current+dt.timedelta(minutes=5)).isoformat(),row['id']))
            db.commit()
            number = f"T{meta['number']:06d}"
            local = dt.datetime.fromisoformat(meta['reminder_at']).astimezone(ZoneInfo(cfg.get('timezone','Asia/Shanghai')))
            # Treat task content as plain quoted text, not executable instructions.
            title = '待办提醒 '+number
            safe_title = str(meta['title']).replace('\n',' ').replace('\r',' ')
            text = f"### {title}\n\n{safe_title}\n\n提醒时间：{local:%Y-%m-%d %H:%M %Z}\n\n截止时间：{meta['due_at'] or '未指定具体时间'}\n\n通知编号：{row['id']}。完成后可回复：完成 {number}"
        try:
            receipt = sender(title,text,cfg['owner_staff_id'])
            state,error,next_at = 'accepted',None,None
        except Exception as exc:
            # Do not persist exception text: HTTP errors may contain credentials or personal identifiers.
            receipt = None
            attempts = row['attempts']+1
            state = 'failed' if attempts >= cfg.get('max_attempts',3) else 'retry'
            error = '推送失败：'+type(exc).__name__
            next_at = (current+dt.timedelta(seconds=60 if attempts==1 else 300)).isoformat()
        with p.locked(), contextlib.closing(p.connect()) as db:
            db.execute('UPDATE personal_deliveries SET state=?,receipt=?,error=?,next_attempt=?,lease_until=NULL WHERE id=?',
                       (state,receipt,error,next_at,row['id']))
        results.append({'notification':row['id'],'state':state})
    backup(current)
    return {'processed':results}

def backup(current):
    directory = p.core.resolve_db_path().parent / 'backups'
    directory.mkdir(mode=0o700,exist_ok=True)
    target = directory / (current.strftime('%Y-%m-%d')+'.sqlite3')
    if not target.exists():
        with p.locked(), contextlib.closing(p.connect()) as source:
            temporary = target.with_suffix('.tmp')
            with sqlite3.connect(temporary) as dest:
                source.backup(dest)
            temporary.chmod(0o600)
            temporary.replace(target)
    for old in sorted(directory.glob('*.sqlite3'))[:-14]:
        old.unlink()

def main():
    result = tick()
    if result.get('processed'):
        print(json.dumps(result,ensure_ascii=False))
if __name__ == '__main__': main()

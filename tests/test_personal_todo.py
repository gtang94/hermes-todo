import datetime as dt
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server-plugin/hermes-todo'))
import personal_todo as p
import personal_notify as n

class PersonalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.env=dict(os.environ)
        os.environ['HERMES_HOME']=self.tmp.name
        os.environ.pop('HERMES_TODO_DB',None)
        path=Path(self.tmp.name)/'hermes-todo'
        path.mkdir()
        (path/'personal-settings.json').write_text(json.dumps({'enabled':True,'owner_staff_id':'test-owner','timezone':'Asia/Shanghai'}))
        self.future=dt.datetime.now(p.UTC)+dt.timedelta(hours=1)
    def tearDown(self):
        os.environ.clear();os.environ.update(self.env);self.tmp.cleanup()
    def create(self,**kw):
        return p.apply({'action':'create','title':'测试','due_at':self.future.isoformat(),**kw})['task']
    def test_dedup_and_conflict(self):
        args={'action':'create','title':'one'}
        a=p.apply(args,'msg1');b=p.apply(args,'msg1')
        self.assertEqual(a,b)
        with self.assertRaises(ValueError):p.apply({**args,'title':'two'},'msg1')
        self.assertEqual(len(p.apply({'action':'list'})['tasks']),1)
    def test_invalid_reminder_rolls_back(self):
        with self.assertRaises(ValueError):self.create(remind_at='2000-01-01T00:00:00Z')
        self.assertEqual(p.core.get_board()['tasks'],[])
    def test_delivery_once_and_title_no_resend(self):
        task=self.create();sent=[]
        clock=self.future+dt.timedelta(minutes=1)
        n.tick(lambda *a:sent.append(a) or 'receipt',clock)
        n.tick(lambda *a:sent.append(a) or 'receipt',clock)
        p.apply({'action':'update','task_id':task['taskNo'],'title':'new'})
        n.tick(lambda *a:sent.append(a) or 'receipt',clock)
        self.assertEqual(len(sent),1)
    def test_complete_cancel_and_reopen(self):
        for action in ('complete','cancel'):
            task=self.create()
            p.apply({'action':action,'task_id':task['taskNo']})
            sent=[];n.tick(lambda *a:sent.append(a),self.future+dt.timedelta(minutes=1))
            self.assertFalse(sent)
            reopened=p.apply({'action':'reopen','task_id':task['taskNo']})['task']
            self.assertIsNone(reopened['reminderAt'])
            self.assertEqual(reopened['status'],'open')
            renamed=p.apply({'action':'update','task_id':task['taskNo'],'title':'renamed'})['task']
            self.assertIsNone(renamed['reminderAt'])
    def test_snooze_preserves_deadline(self):
        task=self.create()
        later=self.future+dt.timedelta(hours=1)
        result=p.apply({'action':'snooze','task_id':task['taskNo'],'remind_at':later.isoformat()})['task']
        self.assertEqual(result['dueAt'],task['dueAt'])
        self.assertEqual(result['reminderAt'],later.isoformat())
        renamed=p.apply({'action':'update','task_id':task['taskNo'],'title':'renamed'})['task']
        self.assertEqual(renamed['reminderAt'],later.isoformat())
    def test_recurring_complete_and_cancel(self):
        task=self.create(recurrence_rule='daily',remind_before_minutes=10)
        result=p.apply({'action':'complete','task_id':task['taskNo']})
        self.assertIsNotNone(result['generatedTask']['reminderAt'])
        result=p.apply({'action':'cancel','task_id':result['generatedTask']['taskNo']})
        self.assertNotIn('generatedTask',result)
        self.assertEqual(result['task']['status'],'cancelled')
    def test_retry_and_expiry(self):
        task=self.create();clock=self.future+dt.timedelta(minutes=1)
        def fail(*a):raise TimeoutError('secret-token')
        for i in (0,2,10):n.tick(fail,clock+dt.timedelta(minutes=i))
        result=p.apply({'action':'get','task_id':task['taskNo']})['task']
        self.assertEqual(result['delivery']['state'],'failed')
        self.assertNotIn('secret-token',json.dumps(result))
        task=self.create(title='old')
        n.tick(fail,clock+dt.timedelta(days=2))
        self.assertEqual(p.apply({'action':'get','task_id':task['taskNo']})['task']['delivery']['state'],'expired')
    def test_priority(self):
        task=self.create(priority=1)
        self.assertEqual(task['priority'],1)
    def test_lease_recovery_is_bounded(self):
        task=self.create();clock=self.future+dt.timedelta(minutes=1)
        with p.connect() as c:
            c.execute("INSERT INTO personal_deliveries(task_id,generation,scheduled_at,state,attempts,lease_until) VALUES(?,1,?,'sending',3,?)",(task['id'],self.future.isoformat(),self.future.isoformat()))
        calls=[];n.tick(lambda *a:calls.append(a),clock)
        self.assertFalse(calls)
        self.assertEqual(p.apply({'action':'get','task_id':task['taskNo']})['task']['delivery']['state'],'failed')
if __name__=='__main__':unittest.main()


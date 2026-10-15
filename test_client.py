import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime
import client
from vault import Database, VaultError

PASSWORD='synthetic-master-password'
TOKEN='synthetic-api-token-0000000000'
LIMITS={'items':[{'type':'full','value':'80'},{'type':'eco','value':'no_limit'},{'type':'voice','value':'not_available'}]}
REPORTS={'items':[{'id':str(i),'status':'delivered' if i<8 else 'undelivered','cost':'0.12','parts':1,'reason':'operator_error',
                   'phone':'+12025550101','text':'SECRET_MESSAGE_NOT_TO_BE_STORED',
                   'sent':'2026-10-13 12:00:00','delivered':'2026-10-13 12:00:12'} for i in range(10)]}

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)/'monitor'; self.m=client.Monitor(self.root)
    def tearDown(self): self.m.close(); self.tmp.cleanup()
    def connect(self):
        self.m.create(PASSWORD)
        with patch.object(client,'api',return_value=LIMITS): self.m.dispatch({'action':'save_account','name':'Synthetic account','token':TOKEN})
    def test_vault_ciphertext_restart_wrong_password_and_no_plaintext_fallback(self):
        self.connect(); raw=self.m.path.read_bytes()
        self.assertFalse(raw.startswith(b'SQLite format')); self.assertNotIn(TOKEN.encode(),raw)
        self.assertEqual(self.m.path.stat().st_mode & 0o777,0o600)
        self.m.close()
        with self.assertRaises(client.Error): self.m.dispatch({'action':'snapshot'})
        with self.assertRaises(VaultError): self.m.dispatch({'action':'unlock','password':'wrong'})
        with patch.object(self.m,'attempts'): self.m.dispatch({'action':'unlock','password':PASSWORD})
        self.assertEqual(self.m.db.get('accounts',[])[0]['token'],TOKEN)
        self.m.close(); self.m.path.unlink()
        with self.assertRaises(client.Error): self.m.dispatch({'action':'snapshot'})
    def test_read_only_endpoint_allowlist(self):
        with patch.object(client,'build_opener') as opener:
            for endpoint in ['messages/send_sms','messages/received','contacts/delete','https://evil.invalid','account/limits?x=1']:
                with self.assertRaises(client.Error): client.api(TOKEN,endpoint)
            opener.assert_not_called()
    def test_only_monitoring_actions(self):
        self.m.create(PASSWORD)
        with patch.object(client,'api') as api:
            for action in ['send','delete_message','read_message','use_standard']:
                with self.assertRaises(client.Error): self.m.dispatch({'action':action})
            api.assert_not_called()
    def test_summaries_and_sensitive_data_excluded_from_storage_and_output(self):
        self.connect()
        with patch.object(client,'api',return_value=REPORTS) as api:
            result=self.m.snapshot()
            api.assert_called_once()
            self.assertEqual(api.call_args.args[1],'messages/reports')
        report=result['accounts'][0]['reports']['data']
        self.assertEqual(report['delivery_percent'],80); self.assertEqual(report['mean_delay'],12)
        self.assertEqual(report['cost'],'1.20'); self.assertEqual(report['failed'],2)
        serialized=json.dumps(result)+str(self.m.db.query('SELECT value FROM documents'))
        self.assertNotIn('SECRET_MESSAGE_NOT_TO_BE_STORED',serialized); self.assertNotIn('+12025550101',serialized)
        self.assertNotIn(TOKEN,json.dumps(result))
    def test_ttl_manual_refresh_and_alert_deduplication(self):
        self.connect()
        with patch.object(client,'api',return_value=REPORTS) as api:
            first=self.m.snapshot(); second=self.m.snapshot(refresh=True)
            self.assertTrue(first['notify']); self.assertFalse(second['notify']); api.assert_called_once()
        cache=self.m.db.get('cache/'+first['accounts'][0]['id'],{})
        self.assertEqual(sorted(cache['alerts']),['delivery_failures','low_full'])
    def test_failure_keeps_last_good_values_and_does_not_claim_freshness(self):
        self.connect()
        with patch.object(client,'api',return_value=REPORTS): first=self.m.snapshot()
        aid=first['accounts'][0]['id']; cache=self.m.db.get('cache/'+aid,{})
        cache['limits']['attempt_at']=0;cache['reports']['attempt_at']=0; self.m.db.put('cache/'+aid,cache)
        with patch.object(client,'api',side_effect=client.Error('Cannot reach the API.')): second=self.m.snapshot()
        account=second['accounts'][0]
        self.assertEqual(account['reports']['data'],cache['reports']['data'])
        self.assertEqual(account['reports']['at'],cache['reports']['at'])
        self.assertTrue(account['reports']['error']); self.assertIn('reports_api',account['alerts'])
    def test_limits_special_values_and_invalid_response(self):
        summary=client.limits_summary(LIMITS)
        self.assertEqual(summary[0]['remaining'],80);self.assertIsNone(summary[1]['remaining'])
        for data in [{},{'items':None},{'items':[{'type':'full','value':'unknown'}]}]:
            with self.assertRaises(client.Error): client.limits_summary(data)
    def test_reports_deduplicate_ignore_missing_cost_and_unknown_reasons(self):
        data={'items':[{'id':'a','status':'undelivered','reason':'secret recipient detail','cost':'NaN'},
                       {'id':'a','status':'undelivered','cost':'99'},{'id':'b','status':'pending'}]}
        result=client.report_summary(data,time.time(),15)
        self.assertEqual(result['total'],2); self.assertIsNone(result['cost']);self.assertIsNone(result['mean_delay'])
        self.assertEqual(result['reasons'],[{'reason':'other_error','count':1}]);self.assertEqual(result['delivery_percent'],0)
    def test_pending_threshold_and_future_scheduled_reports(self):
        now=time.time(); dt=lambda seconds: datetime.fromtimestamp(now+seconds,client.ZONE).isoformat()
        result=client.report_summary({'items':[{'id':'a','status':'pending','queued':dt(-1200)},
          {'id':'b','status':'saved','queued':dt(-1200)},{'id':'c','status':'pending','queued':dt(1200)}]},now,15)
        self.assertEqual(result['old_pending'],1);self.assertEqual(result['pending'],2)
    def test_lock_expiry_and_poll_does_not_extend(self):
        self.m.create(PASSWORD); deadline=self.m.deadline;self.m.snapshot()
        self.assertEqual(deadline,self.m.deadline)
        with patch.object(client.time,'monotonic',return_value=deadline+1):
            with self.assertRaises(client.Error) as err: self.m.snapshot()
        self.assertTrue(err.exception.locked);self.assertIsNone(self.m.db)
    def test_change_password_and_remove_account(self):
        self.connect(); aid=self.m.db.get('accounts',[])[0]['id']
        self.m.dispatch({'action':'password','current_password':PASSWORD,'password':'replacement-password'})
        self.m.dispatch({'action':'remove_account','account':aid})
        self.assertEqual(self.m.db.get('accounts',None),[]);self.assertIsNone(self.m.db.get('cache/'+aid,None))
        self.m.close()
        with self.assertRaises(VaultError): Database(self.m.path,PASSWORD)
        self.m.dispatch({'action':'unlock','password':'replacement-password'})
    def test_symlink_vault_rejected(self):
        self.root.mkdir(); other=Path(self.tmp.name)/'other';other.write_text('synthetic')
        self.m.path.symlink_to(other)
        with self.assertRaises(client.Error): self.m.dispatch({'action':'unlock','password':PASSWORD})
        self.assertEqual(other.read_text(),'synthetic')
    def test_sqlcipher_open_itself_rejects_symlink(self):
        self.m.create(PASSWORD); self.m.close()
        link=Path(self.tmp.name)/'vault-link.db'; link.symlink_to(self.m.path)
        with self.assertRaises(VaultError): Database(link,PASSWORD)
        db=Database(self.m.path,PASSWORD); db.close()
    def test_attempt_counter_symlink_rejected(self):
        self.m.create(PASSWORD); self.m.close()
        other=Path(self.tmp.name)/'counter';other.write_text('{}')
        (self.root/'attempts.json').symlink_to(other)
        with self.assertRaises(OSError): self.m.dispatch({'action':'unlock','password':PASSWORD})
        self.assertEqual(other.read_text(),'{}')
    def test_create_failure_does_not_publish_broken_database(self):
        with patch.object(Database,'put',side_effect=VaultError('test')):
            with self.assertRaises(VaultError): self.m.create(PASSWORD)
        self.assertFalse(self.m.path.exists())
    def test_demo_process_never_creates_storage(self):
        env={**os.environ,'XDG_STATE_HOME':self.tmp.name+'/demo-state'}
        result=subprocess.run([sys.executable,'-I',str(Path(client.__file__)),'--serve','--demo'],
            input=json.dumps({'action':'snapshot','id':1})+'\n',capture_output=True,text=True,env=env,check=True)
        self.assertTrue(json.loads(result.stdout)['ok'])
        self.assertFalse(Path(env['XDG_STATE_HOME']).exists())
        with patch.object(client,'api',side_effect=AssertionError('network')): self.assertTrue(client.demo_snapshot()['accounts'])

if __name__=='__main__': unittest.main()

import concurrent.futures
import contextlib
import copy
import io
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from edbg_cli.main import main
from edbg_sdk import Client
from . import test_operations as base

server=base.server


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base.OperationTests(methodName='runTest');self.fixture.setUp()
        self.opened=self.fixture.open();self.sid=self.opened['session_id'];self.client=self.fixture.client
        with server.db() as c:
            c.execute('UPDATE debug_sessions SET expires=? WHERE id=?',(time.time()+5,self.sid))

    def tearDown(self):self.fixture.tearDown()

    def request(self,revision=1,duration=60000):
        return self.fixture.intent('uart.session.renew',{'session_id':self.sid,'expected_lease_revision':revision,'lease_duration_ms':duration})

    def post(self,document):return self.client.post('/api/workspace/operations',json=document)

    def session(self):
        response=self.client.get('/api/workspace/sessions/'+self.sid);response.raise_for_status();return response.json()

    def test_renewal_is_atomic_idempotent_and_explicitly_server_policy_only(self):
        before=self.session();document=self.request()
        schema=json.loads((server.ROOT.parent/'contracts/compat/uart-session-v0/session.schema.json').read_text())
        Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(before)
        response=self.post(document);response.raise_for_status();result=response.json()
        after=self.session()
        self.assertEqual(result['state'],'finished');self.assertEqual(result['outcome'],'succeeded')
        self.assertEqual(result['result']['confirmation'],'server_policy_only')
        self.assertFalse(result['result']['node_revision_acknowledged'])
        self.assertEqual(after['lease']['revision'],2)
        self.assertGreater(after['lease']['expires_at'],before['lease']['expires_at'])
        self.assertEqual(after['stream_id'],before['stream_id'])
        self.assertEqual(self.post(document).json()['id'],result['id'])
        self.assertEqual(self.session()['lease']['expires_at'],after['lease']['expires_at'])
        self.assertEqual(self.session()['lease']['revision'],2)
        schema=json.loads((server.ROOT.parent/'contracts/compat/uart-operation-v0/result.schema.json').read_text())
        Draft202012Validator(schema).validate(result)

    def test_different_keys_with_same_revision_have_one_winner(self):
        documents=[self.request(),self.request()]
        def send(document):
            with TestClient(server.app) as client:
                client.headers['Origin']='http://testserver';client.cookies.update(self.client.cookies)
                response=client.post('/api/workspace/operations',json=document)
                return response.status_code,response.json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(send,documents))
        self.assertEqual(sorted(r[0] for r in results),[200,409])
        rejected=next(r[1] for r in results if r[0]==409)
        self.assertEqual(rejected['detail']['code'],'LEASE_REVISION_CONFLICT')
        self.assertEqual(self.session()['lease']['revision'],2)

    def test_changed_parameters_same_key_conflict_and_stale_new_key_is_rejected(self):
        document=self.request();self.post(document).raise_for_status()
        changed=copy.deepcopy(document);changed['params']['lease_duration_ms']=120000
        self.assertEqual(self.post(changed).json()['detail']['code'],'IDEMPOTENCY_CONFLICT')
        self.assertEqual(self.post(self.request()).json()['detail']['code'],'LEASE_REVISION_CONFLICT')

    def test_expired_closed_closing_and_lost_contexts_cannot_be_revived(self):
        for state in ('closing','closed','lost'):
            with self.subTest(state=state):
                with server.db() as c:c.execute('UPDATE debug_sessions SET state=? WHERE id=?',(state,self.sid))
                self.assertEqual(self.post(self.request()).status_code,409)
                self.assertEqual(self.session()['lease']['revision'],1)
        with server.db() as c:c.execute("UPDATE debug_sessions SET state='active',expires=? WHERE id=?",(time.time()-1,self.sid))
        self.assertEqual(self.post(self.request()).status_code,409)
        self.assertTrue(self.session()['lease']['expired'])

    def test_new_boot_or_offline_node_rejects_renewal(self):
        with server.db() as c:c.execute('UPDATE devices SET last_seen=0 WHERE id=?',(self.fixture.did,))
        self.assertEqual(self.post(self.request()).status_code,409)
        self.fixture.poll(self.sid,boot='next-boot')
        self.assertEqual(self.post(self.request()).status_code,409)
        self.assertEqual(self.session()['state'],'lost')
        self.assertEqual(self.session()['lease']['revision'],1)

    def test_short_duration_and_invalid_revision_types_are_rejected(self):
        self.assertEqual(self.post(self.request(duration=1000)).json()['detail']['code'],'LEASE_NOT_EXTENDED')
        for revision in (0,True,1.0):
            with self.subTest(revision=revision):self.assertEqual(self.post(self.request(revision=revision)).status_code,422)
        self.assertEqual(self.post(self.request(duration=300001)).status_code,422)
        self.assertEqual(self.session()['lease']['revision'],1)

    def test_close_wins_over_late_renewal_and_original_retry_does_not_reopen(self):
        document=self.request();accepted=self.post(document).json()
        deadline=self.session()['lease']['expires_at']
        self.fixture.close(self.sid)
        self.assertEqual(self.post(document).json()['id'],accepted['id'])
        self.assertEqual(self.session()['state'],'closed')
        self.assertEqual(self.session()['lease']['expires_at'],deadline)
        self.assertEqual(self.post(self.request(revision=2)).status_code,409)

    def test_racing_close_and_renew_never_restores_active_state(self):
        renew=self.request();closing=self.fixture.intent('uart.session.close',{'session_id':self.sid})
        def send(document):
            with TestClient(server.app) as client:
                client.headers['Origin']='http://testserver';client.cookies.update(self.client.cookies)
                return client.post('/api/workspace/operations',json=document).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:statuses=list(pool.map(send,(renew,closing)))
        self.assertEqual(statuses[1],200);self.assertIn(statuses[0],(200,409))
        self.assertEqual(self.session()['state'],'closing')
        self.assertEqual(self.post(self.request(revision=self.session()['lease']['revision'])).status_code,409)

    def test_renewal_does_not_reset_write_deadlines_or_unknown_results(self):
        written,_=self.fixture.write(self.sid)
        before=self.fixture.get(written['id'])
        self.post(self.request()).raise_for_status()
        self.assertEqual(self.fixture.get(written['id'])['dispatch_before'],before['dispatch_before'])
        self.fixture.poll(self.sid)
        with server.db() as c:c.execute('UPDATE workspace_operations SET execution_deadline=? WHERE id=?',(time.time()-1,written['id']))
        self.assertEqual(self.fixture.get(written['id'])['outcome'],'unknown')
        self.post(self.request(revision=2,duration=120000)).raise_for_status()
        self.assertEqual(self.fixture.get(written['id'])['outcome'],'unknown')
        self.assertNotIn('tx',self.fixture.poll(self.sid))

    def test_idle_or_foreign_context_report_does_not_reopen_a_managed_session(self):
        self.assertIsNone(self.fixture.poll('f'*24))
        self.assertEqual(self.session()['state'],'closing')
        self.assertEqual(self.post(self.request()).status_code,409)
        self.assertIsNone(self.fixture.poll(self.sid))
        self.assertEqual(self.session()['state'],'closing')
        self.assertIsNone(self.fixture.poll())
        self.assertEqual(self.session()['state'],'closed')
        self.assertEqual(self.post(self.request()).status_code,409)

    def test_missing_snapshot_does_not_publish_queued_bytes_or_claim_release(self):
        written,_=self.fixture.write(self.sid)
        body={'boot_id':'test-boot','version':'3.5.0','partition':'app0','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1']}
        response=self.client.post('/api/device/'+self.fixture.did+'/poll',json=body,headers={'Authorization':'Bearer '+self.fixture.token})
        response.raise_for_status();self.assertIsNone(response.json()['debug_control'])
        self.assertEqual(self.session()['state'],'closing')
        self.assertEqual(self.fixture.get(written['id'])['result']['effect'],'none')
        self.assertEqual(self.fixture.get(self.opened['id'])['resource_status'],'held')

    def test_session_queries_are_read_only_and_cli_renewal_uses_same_ledger(self):
        factory=lambda url,**kw:Client('http://testserver',http_client=self.client)
        with server.db() as c:before='\n'.join(c.iterdump())
        self.session();self.client.get('/api/workspace/sessions',params={'node':self.fixture.did}).raise_for_status()
        with server.db() as c:self.assertEqual(before,'\n'.join(c.iterdump()))
        output=io.StringIO()
        with contextlib.redirect_stdout(output):code=main(['session','show',self.sid,'--json'],client_factory=factory)
        self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['data']['lease']['revision'],1)
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'renew.json';path.write_text(json.dumps(self.request()),encoding='utf-8')
            output=io.StringIO()
            with contextlib.redirect_stdout(output):code=main(['operation','submit','--file',str(path),'--wait','--json'],client_factory=factory)
            self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['data']['result']['lease_revision'],2)

    def test_new_configuration_revision_blocks_renewal_but_allows_cleanup(self):
        self.fixture.apply('connections',dict(self.fixture.connection,enabled=False),1).raise_for_status()
        self.assertEqual(self.post(self.request()).status_code,409)
        self.assertEqual(self.fixture.close(self.sid)['outcome'],'succeeded')

    def test_failed_acceptance_rolls_back_lease_change_and_operation_together(self):
        before=self.session();document=self.request()
        with patch.object(server.operations,'change',side_effect=RuntimeError('Simulated transaction interruption')):
            with self.assertRaises(RuntimeError):self.post(document)
        after=self.session()
        self.assertEqual(after['lease'],before['lease'] | {'remaining_ms':after['lease']['remaining_ms']})
        with server.db() as c:
            self.assertIsNone(c.execute('SELECT id FROM workspace_operations WHERE idempotency_key=?',(document['idempotency_key'],)).fetchone())
        result=self.post(document);result.raise_for_status()
        self.assertEqual(result.json()['result']['lease_revision'],2)


if __name__=='__main__':unittest.main()

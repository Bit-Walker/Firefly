import concurrent.futures
import contextlib
import copy
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from edbg_sdk import Client
from edbg_cli.main import main
from edbg_server import operations
from . import test_workspace as base
from . import agent_runtime

server = base.server


class OperationTests(unittest.TestCase):
    apply = base.WorkspaceTests.apply
    tearDown = base.WorkspaceTests.tearDown

    def setUp(self):
        base.WorkspaceTests.setUp(self)
        self.request['idempotency_key'] = secrets.token_hex(16)
        self.token = secrets.token_hex(24)
        with server.db() as c:
            c.execute('UPDATE devices SET token_hash=? WHERE id=?', (server.digest(self.token), self.did))

    def intent(self, action, params, **changes):
        result = copy.deepcopy(self.request)
        result.update(action=action, params=params, idempotency_key=secrets.token_hex(16))
        result['timing']['execution_timeout_ms'] = 10000 if action == 'uart.write' else 30000
        result.update(changes)
        return result

    def submit(self, document=None):
        response = self.client.post('/api/workspace/operations', json=document or self.request)
        response.raise_for_status()
        return response.json()

    def get(self, identity):
        response = self.client.get('/api/workspace/operations/' + identity)
        response.raise_for_status()
        return response.json()

    def poll(self, sid='', tx_id='', tx_error='', state=None, boot='test-boot'):
        body = {'boot_id': boot, 'version': '3.5.0', 'runtime':agent_runtime(), 'partition': 'app0', 'uptime': 1,
                'heap': 100000, 'min_heap': 90000, 'rssi': -40, 'capabilities': ['debug_uart_v1','i2c_master_v2'],
                'debug': {'session': sid, 'state': state or ('active' if sid else 'idle'), 'tx_id': tx_id, 'tx_error': tx_error}}
        response = self.client.post('/api/device/' + self.did + '/poll', json=body, headers={'Authorization': 'Bearer ' + self.token})
        response.raise_for_status()
        return response.json()['debug_control']

    def open(self):
        result = self.submit()
        sid = result['session_id']
        self.assertEqual(self.poll()['session'], sid)
        self.poll(sid)
        result = self.get(result['id'])
        self.assertEqual(result['outcome'], 'succeeded')
        return result

    def write(self, sid, data='6869'):
        document = self.intent('uart.write', {'session_id': sid, 'data_hex': data})
        return self.submit(document), document

    def close(self, sid):
        result = self.submit(self.intent('uart.session.close', {'session_id': sid}))
        self.assertIsNone(self.poll(sid))
        self.poll()
        return self.get(result['id'])

    def test_open_write_confirm_close_and_shared_legacy_exclusion(self):
        opened = self.open(); sid = opened['session_id']
        self.assertEqual(self.client.post('/api/debug/sessions', json={'device_id': self.did}).status_code, 409)
        self.assertEqual(self.client.post('/api/debug/sessions/'+sid+'/close').status_code, 409)
        written, _ = self.write(sid)
        tx_id = self.poll(sid)['tx']['id']
        self.assertNotIn('tx', self.poll(sid))
        self.poll(sid, tx_id)
        finished = self.get(written['id'])
        self.assertEqual(finished['outcome'], 'succeeded')
        self.assertEqual(finished['result']['confirmed_bytes'], 2)
        self.assertEqual(finished['evidence'][-1]['level'], 'driver_report')
        self.assertFalse(finished['compatibility']['node_execution_budget_enforced'])
        schema_path = operations.planning.WORKSPACE_ROOT / 'contracts/compat/uart-operation-v0/result.schema.json'
        schema = json.loads(schema_path.read_text(encoding='utf-8'))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(written)
        Draft202012Validator(schema).validate(finished)
        closed = self.close(sid)
        self.assertEqual(closed['outcome'], 'succeeded')
        self.assertEqual(self.get(opened['id'])['resource_status'], 'released')

    def test_same_key_concurrent_and_after_relogin_returns_one_operation(self):
        def submit_again(_):
            with TestClient(server.app) as c:
                c.headers['Origin'] = 'http://testserver'
                c.post('/api/login', json=server.ADMIN).raise_for_status()
                response = c.post('/api/workspace/operations', json=self.request)
                response.raise_for_status()
                return response.json()['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(submit_again, (1, 2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.submit()['id'], ids[0])
        changed = copy.deepcopy(self.request); changed['params']['lease_duration_ms'] -= 1000
        response = self.client.post('/api/workspace/operations', json=changed)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'IDEMPOTENCY_CONFLICT')
        with server.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_operations WHERE node_id=?', (self.did,)).fetchone()[0], 1)

    def test_hex_case_normalizes_but_changed_bytes_conflict(self):
        sid = self.open()['session_id']
        result, document = self.write(sid, 'aB')
        document['params']['data_hex'] = 'Ab'
        self.assertEqual(self.submit(document)['id'], result['id'])
        document['params']['data_hex'] = 'ac'
        self.assertEqual(self.client.post('/api/workspace/operations', json=document).status_code, 409)

    def test_configuration_change_does_not_rebind_accepted_intent_or_prevent_cleanup(self):
        result = self.submit()
        self.apply('connections', dict(self.connection, enabled=False), 1).raise_for_status()
        self.assertEqual(self.submit()['id'], result['id'])
        sid = result['session_id']; self.poll(); self.poll(sid)
        self.assertEqual(self.get(result['id'])['binding']['connection']['revision'], 1)
        self.assertEqual(self.close(sid)['outcome'], 'succeeded')

    def test_expiry_before_publication_does_not_send_or_leave_a_lock(self):
        result = self.submit()
        with server.db() as c:
            c.execute('UPDATE workspace_operations SET dispatch_before=? WHERE id=?', (time.time()-1, result['id']))
        finished = self.get(result['id'])
        self.assertEqual(finished['outcome'], 'expired')
        self.assertEqual(finished['result']['effect'], 'none')
        self.assertEqual(finished['resource_status'], 'released')
        self.assertIsNone(self.poll())

    def test_lost_response_is_not_retransmitted_and_late_confirmation_revises_unknown(self):
        sid = self.open()['session_id']
        result, document = self.write(sid)
        tx_id = self.poll(sid)['tx']['id']  # Simulate losing this HTTP response.
        self.assertNotIn('tx', self.poll(sid))
        self.assertEqual(self.submit(document)['id'], result['id'])
        with server.db() as c:
            c.execute('UPDATE workspace_operations SET execution_deadline=? WHERE id=?', (time.time()-1, result['id']))
        unknown = self.get(result['id'])
        self.assertEqual(unknown['outcome'], 'unknown')
        self.assertEqual(unknown['resource_status'], 'unconfirmed')
        another = self.intent('uart.write', {'session_id':sid,'data_hex':'00'})
        self.assertEqual(self.client.post('/api/workspace/operations', json=another).status_code, 409)
        self.assertNotIn('tx', self.poll(sid))
        self.poll(sid, tx_id)
        reconciled = self.get(result['id'])
        self.assertEqual(reconciled['outcome'], 'succeeded')
        self.assertGreater(reconciled['revision'], unknown['revision'])
        events = self.client.get('/api/workspace/operations/'+result['id']+'/events').json()['items']
        self.assertIn('unknown', [e['outcome'] for e in events])
        self.assertEqual(events[-1]['outcome'], 'succeeded')

    def test_new_boot_blocks_queued_write_and_never_replays_dispatched_write(self):
        sid = self.open()['session_id']
        result, _ = self.write(sid)
        self.assertIsNone(self.poll(sid, boot='new-boot'))
        finished = self.get(result['id'])
        self.assertEqual(finished['outcome'], 'failed')
        self.assertEqual(finished['result']['effect'], 'none')
        self.assertEqual(finished['resource_status'], 'released')

    def test_reboot_after_publication_preserves_unknown_and_releases_old_context(self):
        sid = self.open()['session_id']
        result, _ = self.write(sid)
        self.poll(sid)
        self.assertIsNone(self.poll(boot='new-boot'))
        finished = self.get(result['id'])
        self.assertEqual(finished['outcome'], 'unknown')
        self.assertEqual(finished['resource_status'], 'released')

    def test_cancellation_before_and_after_publication_uses_different_semantics(self):
        pending = self.submit()
        cancelled = self.client.post('/api/workspace/operations/'+pending['id']+'/cancel').json()
        self.assertEqual(cancelled['outcome'], 'cancelled')
        self.assertIsNone(self.poll())
        self.request['idempotency_key'] = secrets.token_hex(16)
        sid = self.open()['session_id']
        written, _ = self.write(sid)
        tx_id = self.poll(sid)['tx']['id']
        response = self.client.post('/api/workspace/operations/'+written['id']+'/cancel').json()
        self.assertTrue(response['cancel_requested']); self.assertIsNone(response['outcome'])
        self.poll(sid, tx_id)
        self.assertEqual(self.get(written['id'])['outcome'], 'succeeded')

    def test_acceptance_survives_a_separate_service_process(self):
        result = self.submit()
        env = dict(os.environ, FLEET_STATE=str(server.STATE))
        code = '''import sys,json
from edbg_server import app as server
from fastapi.testclient import TestClient
with TestClient(server.app) as client:
 client.headers['Origin']='http://testserver'
 client.post('/api/login',json=server.ADMIN).raise_for_status()
 response=client.get('/api/workspace/operations/'+sys.argv[1]);response.raise_for_status()
 print(json.dumps(response.json()))
'''
        process = subprocess.run([sys.executable, '-c', code, result['id']], env=env, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)['id'], result['id'])
        self.assertEqual(self.submit()['id'], result['id'])

    def test_cli_wait_timeout_retains_operation_id_and_event_cursor(self):
        factory = lambda url, **kw: Client('http://testserver', http_client=self.client)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'request.json'; path.write_text(json.dumps(self.request), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(['operation','submit','--file',str(path),'--wait','--wait-timeout','0.02','--json'],client_factory=factory)
            result = json.loads(output.getvalue())['data']
        self.assertEqual(code, 8); self.assertTrue(result['wait_timed_out'])
        self.assertEqual(result['state'], 'accepted')
        page = self.client.get('/api/workspace/operations/'+result['id']+'/events?limit=1').json()
        self.assertEqual(page['items'][0]['seq'], 1)
        self.assertEqual(self.client.get('/api/workspace/operations/'+result['id']+'/events?after=1').json()['items'], [])

    def test_dispatched_write_is_not_republished_after_service_process_restart(self):
        sid = self.open()['session_id']
        result, _ = self.write(sid)
        tx_id = self.poll(sid)['tx']['id']
        env = dict(os.environ, FLEET_STATE=str(server.STATE))
        code = '''import sys,json
from edbg_server import app as server
from fastapi.testclient import TestClient
body={'boot_id':'test-boot','version':'3.5.0','partition':'app0','uptime':2,'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1'], 'debug':{'session':sys.argv[3],'state':'active'}}
with server.db() as c: body['runtime']=json.loads(c.execute('SELECT telemetry FROM devices WHERE id=?',(sys.argv[1],)).fetchone()[0])['runtime']
with TestClient(server.app) as client:
 response=client.post('/api/device/'+sys.argv[1]+'/poll',json=body,headers={'Authorization':'Bearer '+sys.argv[2]});response.raise_for_status()
 print(json.dumps(response.json()))
'''
        process = subprocess.run([sys.executable, '-c', code, self.did, self.token, sid], env=env, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn('tx', json.loads(process.stdout)['debug_control'])
        self.assertEqual(self.get(result['id'])['state'], 'dispatched')
        self.poll(sid, tx_id)
        self.assertEqual(self.get(result['id'])['outcome'], 'succeeded')

    def test_driver_error_never_fabricates_a_confirmed_byte_count(self):
        sid = self.open()['session_id']
        result, _ = self.write(sid)
        tx_id = self.poll(sid)['tx']['id']
        self.poll(sid, tx_id, tx_error='UART transmit incomplete; do not retry automatically')
        final = self.get(result['id'])
        self.assertEqual(final['outcome'], 'failed')
        self.assertIsNone(final['result']['confirmed_bytes'])
        self.assertEqual(final['result']['effect'], 'possible')

    def test_cancelled_queued_write_and_operation_pagination(self):
        sid = self.open()['session_id']
        result, _ = self.write(sid)
        self.assertEqual(self.client.post('/api/workspace/operations/'+result['id']+'/cancel').json()['outcome'], 'cancelled')
        self.assertNotIn('tx', self.poll(sid))
        first = self.client.get('/api/workspace/operations', params={'node':self.did,'limit':1}).json()
        second = self.client.get('/api/workspace/operations', params={'node':self.did,'limit':1,'before':first['next_before']}).json()
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])


if __name__ == '__main__': unittest.main()

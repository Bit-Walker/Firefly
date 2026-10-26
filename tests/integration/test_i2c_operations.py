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
from unittest.mock import patch

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from edbg_cli.main import main
from edbg_sdk import Client
from . import test_operations as base
from . import agent_runtime

server=base.server


class I2COperationTests(unittest.TestCase):
    def setUp(self):
        self.fixtures=[];self.fixture,self.request=self.create_fixture()
        self.client=self.fixture.client

    def create_fixture(self):
        fixture=base.OperationTests(methodName='runTest');fixture.setUp();self.fixtures.append(fixture)
        target={'api_version':'edbg.target/v0','id':'sensor-'+fixture.did,'name':'Declared test peripheral','kind':'external'}
        fixture.apply('targets',target,0).raise_for_status()
        connection=dict(fixture.connection,target={'id':target['id'],'revision':1},interface={'id':'i2c0','kind':'i2c'},physical_bus_id='test-shared-i2c')
        fixture.apply('connections',connection,1).raise_for_status()
        document={'api_version':'edbg.operation.draft/0','connection':{'id':connection['id'],'revision':2},'action':'i2c.transfer',
            'params':{'address':0x55,'speed_hz':10000,'messages':[{'write_hex':'d0'},{'read_length':1}]},
            'idempotency_key':secrets.token_hex(16),'timing':{'dispatch_within_ms':5000,'execution_timeout_ms':10000}}
        return fixture,document

    def tearDown(self):
        for fixture in self.fixtures:
            with server.db() as c:
                c.execute("UPDATE debug_sessions SET state='closed' WHERE device_id=?",(fixture.did,))
                server.operations.refresh(c,time.time(),fixture.did)
            fixture.tearDown()

    def submit(self,document=None,client=None):
        response=(client or self.client).post('/api/workspace/operations',json=document or self.request)
        response.raise_for_status();return response.json()

    def get(self,identity):return self.fixture.get(identity)

    def poll(self,sid='',report=None,boot='test-boot',fixture=None):
        fixture=fixture or self.fixture
        body={'boot_id':boot,'version':'3.5.0','runtime':agent_runtime(),'partition':'app0','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40,
            'capabilities':['debug_uart_v1','i2c_master_v2'],'debug':{'session':sid,'state':'active' if sid else 'idle'}}
        if report is not None:body['debug']['i2c']=report
        response=fixture.client.post('/api/device/'+fixture.did+'/poll',json=body,headers={'Authorization':'Bearer '+fixture.token})
        response.raise_for_status();return response.json()['debug_control']

    def prepare(self,document=None):
        accepted=self.submit(document);sid=accepted['session_id']
        self.assertEqual(self.poll()['session'],sid)
        command=self.poll(sid)['i2c']
        self.assertEqual(command['kind'],'master');self.assertEqual(command['mode'],'external')
        return accepted,command

    def done(self,accepted,command,data='ab',state='done',error='',**extra):
        report={'id':command['id'],'state':state,'data':data,'error':error,**extra}
        self.assertIsNone(self.poll(accepted['session_id'],report))
        pending=self.get(accepted['id']);self.assertEqual(pending['state'],'releasing')
        self.poll(report=report)
        return self.get(accepted['id'])

    def test_write_read_once_then_cleanup_and_contract(self):
        accepted,command=self.prepare()
        self.assertEqual(command['operation'],'write_read');self.assertEqual(command['data'],'d0')
        self.assertIsNone(self.poll(accepted['session_id']).get('i2c'))
        final=self.done(accepted,command)
        self.assertEqual(final['outcome'],'succeeded');self.assertEqual(final['resource_status'],'released')
        self.assertEqual(final['result']['read_data_hex'],'ab');self.assertTrue(final['result']['cleanup_confirmed'])
        self.assertEqual(final['result']['confirmed_write_bytes'],1);self.assertEqual(final['result']['confirmed_read_bytes'],1)
        self.assertEqual(final['compatibility']['level'],'legacy_i2c_once')
        schema=json.loads((server.ROOT.parent/'contracts/compat/uart-operation-v0/result.schema.json').read_text())
        Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(accepted);Draft202012Validator(schema).validate(final)

    def test_single_write_and_single_read_are_supported(self):
        for messages,expected,data in [([{'write_hex':'0102'}],'write',''),([{'read_length':2}],'read','beef')]:
            document=copy.deepcopy(self.request);document['idempotency_key']=secrets.token_hex(16);document['params']['messages']=messages
            accepted,command=self.prepare(document)
            self.assertEqual(command['operation'],expected)
            self.assertEqual(self.done(accepted,command,data=data)['outcome'],'succeeded')

    def test_nack_timeout_and_bus_not_idle_preserve_progress_uncertainty(self):
        cases=[('NACK: address or data not acknowledged','I2C_NACK',{},None),
               ('ESP_ERR_TIMEOUT','I2C_TIMEOUT',{},None),('Bus not idle: SDA/SCL held low','I2C_BUS_NOT_IDLE',{'sda_idle':0,'scl_idle':1},0)]
        for error,code,extra,count in cases:
            document=copy.deepcopy(self.request);document['idempotency_key']=secrets.token_hex(16)
            accepted,command=self.prepare(document)
            final=self.done(accepted,command,data='',state='failed',error=error,**extra)
            self.assertEqual(final['outcome'],'failed');self.assertEqual(final['result']['code'],code)
            self.assertEqual(final['result']['confirmed_write_bytes'],count)
            self.assertFalse(final['result']['read_complete']);self.assertEqual(final['result']['read_data_hex'],'')

    def test_duplicate_key_case_normalization_and_conflicting_payload(self):
        document=copy.deepcopy(self.request);document['params']['messages'][0]['write_hex']='AB'
        accepted=self.submit(document);document['params']['messages'][0]['write_hex']='ab'
        self.assertEqual(self.submit(document)['id'],accepted['id'])
        changed=copy.deepcopy(document);changed['params']['messages'][0]['write_hex']='ac'
        response=self.client.post('/api/workspace/operations',json=changed)
        self.assertEqual(response.json()['detail']['code'],'IDEMPOTENCY_CONFLICT')
        with server.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_operations WHERE node_id=?',(self.fixture.did,)).fetchone()[0],1)

    def test_global_guard_blocks_other_node_and_legacy_mutations(self):
        accepted,command=self.prepare()
        other,document=self.create_fixture()
        response=other.client.post('/api/workspace/operations',json=document)
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.client.post('/api/i2c',json={'mode':'external','operation':'read','master_session':'a'*24,'read_len':1,'request_id':secrets.token_hex(12)}).status_code,409)
        self.assertEqual(self.client.post('/api/i2c/'+command['id']+'/cancel').status_code,409)
        self.assertEqual(self.client.post('/api/debug/sessions/'+accepted['session_id']+'/close').status_code,409)
        self.done(accepted,command)
        self.assertEqual(other.client.post('/api/workspace/operations',json=document).status_code,200)

    def test_cleanup_unconfirmed_blocks_bus_even_after_master_completed(self):
        accepted,command=self.prepare();report={'id':command['id'],'state':'done','data':'ab'}
        self.assertIsNone(self.poll(accepted['session_id'],report))
        with server.db() as c:c.execute('UPDATE workspace_i2c_operations SET cleanup_deadline=? WHERE operation_id=?',(time.time()-1,accepted['id']))
        unknown=self.get(accepted['id'])
        self.assertEqual(unknown['outcome'],'unknown');self.assertEqual(unknown['resource_status'],'unconfirmed')
        self.assertEqual(unknown['result']['bus_outcome'],'succeeded')
        other,document=self.create_fixture()
        self.assertEqual(other.client.post('/api/workspace/operations',json=document).status_code,409)
        self.poll(report=report)
        final=self.get(accepted['id'])
        self.assertEqual(final['outcome'],'succeeded');self.assertGreater(final['revision'],unknown['revision'])

    def test_lost_publication_is_not_replayed_and_late_report_reconciles_original(self):
        accepted,command=self.prepare()
        self.assertIsNone(self.poll(accepted['session_id']).get('i2c'))
        self.assertEqual(self.submit()['id'],accepted['id'])
        with server.db() as c:c.execute('UPDATE workspace_operations SET execution_deadline=? WHERE id=?',(time.time()-1,accepted['id']))
        self.assertEqual(self.get(accepted['id'])['state'],'releasing')
        self.poll()
        unknown=self.get(accepted['id']);self.assertEqual(unknown['outcome'],'unknown')
        self.poll(report={'id':command['id'],'state':'done','data':'ab'})
        final=self.get(accepted['id']);self.assertEqual(final['outcome'],'succeeded')
        self.assertGreater(final['revision'],unknown['revision'])

    def test_reboot_before_bus_publication_and_after_publication(self):
        accepted=self.submit();self.assertIsNone(self.poll(boot='new-boot'))
        final=self.get(accepted['id']);self.assertEqual(final['outcome'],'failed');self.assertEqual(final['result']['bus_effect'],'none')
        self.poll(boot='test-boot')
        document=copy.deepcopy(self.request);document['idempotency_key']=secrets.token_hex(16)
        accepted,command=self.prepare(document)
        self.assertIsNone(self.poll(report={'id':command['id'],'state':'done','data':'ab'},boot='newer-boot'))
        self.assertEqual(self.get(accepted['id'])['outcome'],'unknown')

    def test_cancel_before_preparation_and_after_bus_publication(self):
        accepted=self.submit()
        cancelled=self.client.post('/api/workspace/operations/'+accepted['id']+'/cancel').json()
        self.assertEqual(cancelled['outcome'],'cancelled');self.assertEqual(cancelled['resource_status'],'released')
        self.assertIsNone(self.poll())
        document=copy.deepcopy(self.request);document['idempotency_key']=secrets.token_hex(16)
        accepted,command=self.prepare(document)
        result=self.client.post('/api/workspace/operations/'+accepted['id']+'/cancel').json()
        self.assertTrue(result['cancel_requested']);self.assertEqual(result['state'],'releasing')
        self.poll(report={'id':command['id'],'state':'done','data':'ab'})
        self.assertEqual(self.get(accepted['id'])['outcome'],'succeeded')

    def test_partial_preparation_cancel_stops_context_without_bus_commands(self):
        accepted=self.submit();sid=accepted['session_id'];self.poll()
        result=self.client.post('/api/workspace/operations/'+accepted['id']+'/cancel').json()
        self.assertEqual(result['state'],'releasing');self.assertEqual(result['result']['bus_effect'],'none')
        self.assertEqual(result['result']['effect'],'preparation_only')
        self.assertIsNone(self.poll(sid));self.poll()
        self.assertEqual(self.get(accepted['id'])['outcome'],'cancelled')

    def test_bad_report_length_does_not_claim_complete_read(self):
        accepted,command=self.prepare();final=self.done(accepted,command,data='')
        self.assertEqual(final['outcome'],'failed');self.assertEqual(final['result']['code'],'I2C_REPORT_LENGTH_MISMATCH')
        self.assertIsNone(final['result']['confirmed_read_bytes'])

    def test_unsupported_message_shape_rejects_before_creating_context(self):
        document=copy.deepcopy(self.request);document['params']['messages'].append({'read_length':1})
        self.assertEqual(self.client.post('/api/workspace/operations',json=document).status_code,409)
        with server.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM debug_sessions WHERE device_id=?',(self.fixture.did,)).fetchone()[0],0)

    def test_acceptance_failure_rolls_back_context_ledger_and_global_guard(self):
        with patch.object(server.operations,'change',side_effect=RuntimeError('Simulated acceptance interruption')):
            with self.assertRaises(RuntimeError):self.submit()
        with server.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM debug_sessions WHERE device_id=?',(self.fixture.did,)).fetchone()[0],0)
            self.assertIsNone(c.execute('SELECT id FROM workspace_operations WHERE idempotency_key=?',(self.request['idempotency_key'],)).fetchone())
        self.submit()

    def test_new_service_process_does_not_publish_a_dispatched_transaction_again(self):
        accepted,command=self.prepare();env=dict(os.environ,FLEET_STATE=str(server.STATE))
        code='''import sys,json
from edbg_server import app as server
from fastapi.testclient import TestClient
body={'boot_id':'test-boot','version':'3.5.0','partition':'app0','uptime':2,'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1','i2c_master_v2'],'debug':{'session':sys.argv[3],'state':'active'}}
with server.db() as c: body['runtime']=json.loads(c.execute('SELECT telemetry FROM devices WHERE id=?',(sys.argv[1],)).fetchone()[0])['runtime']
with TestClient(server.app) as client:
 response=client.post('/api/device/'+sys.argv[1]+'/poll',json=body,headers={'Authorization':'Bearer '+sys.argv[2]});response.raise_for_status();print(json.dumps(response.json()))
'''
        process=subprocess.run([sys.executable,'-c',code,self.fixture.did,self.fixture.token,accepted['session_id']],env=env,capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr)
        self.assertIsNone(json.loads(process.stdout)['debug_control'].get('i2c'))
        self.assertEqual(self.get(accepted['id'])['state'],'dispatched')

    def test_different_nodes_racing_for_adapter_have_one_winner(self):
        other,document=self.create_fixture()
        with server.db() as c:
            config=server.workspace.record(c,'connection',document['connection']['id'])
        configuration={k:v for k,v in config.items() if k not in ('revision','source','created')}
        configuration['physical_bus_id']='different-looking-bus'
        other.apply('connections',configuration,2).raise_for_status();document['connection']['revision']=3
        def send(pair):
            fixture,request=pair
            with TestClient(server.app) as client:
                client.headers['Origin']='http://testserver';client.cookies.update(fixture.client.cookies)
                return client.post('/api/workspace/operations',json=request).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            codes=list(pool.map(send,((self.fixture,self.request),(other,document))))
        self.assertEqual(sorted(codes),[200,409])

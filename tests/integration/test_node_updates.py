import concurrent.futures
import contextlib
import copy
import hashlib
import io
import json
import os
import secrets
import sqlite3
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
from edbg_sdk import Client, ClientError
from edbg_server import artifacts, migrations, node_updates
from . import test_workspace as base
from . import agent_runtime

server=base.server


def fake_image(version='3.5.0',payload=b'isolated simulated image'):
    import struct
    from functools import reduce
    from operator import xor
    header=bytearray(24);header[0]=0xe9;header[1]=1;header[12:14]=(9).to_bytes(2,'little');header[23]=1
    segment=bytearray(256);segment[0:4]=bytes.fromhex('3254cdab')
    segment[16:16+len(version)]=version.encode();segment[48:59]=b'fleet_agent'
    segment.extend(payload);segment.extend(b'\0'*(-len(segment)%4))
    image=header+struct.pack('<II',0x3c000020,len(segment))+segment
    checksum=reduce(xor,segment,0xef);image.extend(b'\0'*(-(len(image)+1)%16));image.append(checksum)
    return bytes(image)+hashlib.sha256(image).digest()


class NodeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture=base.WorkspaceTests(methodName='runTest');self.fixture.setUp()
        self.client=self.fixture.client;self.did=self.fixture.did;self.token=secrets.token_hex(24)
        with server.db() as c:
            c.execute('UPDATE devices SET token_hash=?,partition=? WHERE id=?',(server.digest(self.token),'ota_0',self.did))
        connection=dict(self.fixture.connection,interface={'id':'agent','kind':'maintenance'})
        self.fixture.apply('connections',connection,1).raise_for_status()
        response=self.client.post('/api/workspace/artifacts',content=fake_image(payload=self.did.encode()))
        response.raise_for_status();self.image=response.json()
        self.request={'api_version':'edbg.operation.draft/0','connection':{'id':connection['id'],'revision':2},
            'action':'node.update','params':{'image':{'artifact_id':self.image['id'],'sha256':self.image['sha256']},'confirmation':'image_identity'},
            'idempotency_key':secrets.token_hex(16),'timing':{'dispatch_within_ms':10000,'execution_timeout_ms':300000}}

    def tearDown(self):
        with server.db() as c:
            c.execute('UPDATE workspace_update_operations SET held=0 WHERE node_id=?',(self.did,))
            c.execute("UPDATE workspace_operations SET state='finished',outcome='failed',resource_status='released' WHERE node_id=? AND state!='finished'",(self.did,))
            c.execute("UPDATE jobs SET state='cancelled' WHERE device_id=? AND state IN ('queued','dispatched','running','rebooting')",(self.did,))
            c.execute("UPDATE debug_sessions SET state='closed' WHERE device_id=?",(self.did,))
        self.fixture.tearDown()

    def submit(self,request=None):
        response=self.client.post('/api/workspace/operations',json=request or self.request)
        response.raise_for_status();return response.json()

    def get(self,identity):
        response=self.client.get('/api/workspace/operations/'+identity);response.raise_for_status();return response.json()

    def poll(self,boot='test-boot',version='3.5.0',report=None):
        body={'boot_id':boot,'version':version,'partition':'ota_0' if boot=='test-boot' else 'ota_1','uptime':1,
            'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1','i2c_master_v2'],
            'debug':{'session':'','state':'idle'}}
        body['runtime']=agent_runtime(version,image={'algorithm':'sha256-file-v1','sha256':self.image['sha256'],'size':self.image['size'],'project':'fleet_agent'} if boot!='test-boot' else None)
        if report is not None:body['report']=report
        return self.client.post('/api/device/'+self.did+'/poll',json=body,headers={'Authorization':'Bearer '+self.token})

    def dispatch(self):
        operation=self.submit();response=self.poll();response.raise_for_status()
        return operation,response.json()['command']

    def report(self,command,state='succeeded',**kwargs):
        return self.poll(report={'id':command['id'],'state':state,'progress':100 if state=='succeeded' else 20,'detail':'isolated report'},**kwargs)

    def test_catalog_content_identity_pagination_and_withdrawal_persistence(self):
        self.client.post('/api/workspace/artifacts',content=fake_image(payload=(self.did+'second').encode())).raise_for_status()
        data=fake_image(payload=self.did.encode())
        repeated=self.client.post('/api/workspace/artifacts',content=data).json()
        self.assertEqual(repeated,self.image)
        self.assertEqual(self.image['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(self.image['validation'],'esp_image_segments_checksum_sha256');self.assertFalse(self.image['signature_verified'])
        schema=json.loads((Path(server.ROOT).parent/'contracts/compat/artifact-v0/manifest.schema.json').read_text())
        Draft202012Validator(schema).validate(self.image)
        page=self.client.get('/api/workspace/artifacts?limit=1').json();self.assertEqual(len(page['items']),1)
        self.assertIsNotNone(page['next_before'])
        self.assertEqual(self.client.get('/api/workspace/artifacts/no-such-image').status_code,404)
        self.client.post('/api/firmware/'+self.image['id']+'/withdraw',json={'reason':'isolated withdrawal'}).raise_for_status()
        repeated=self.client.post('/api/workspace/artifacts',content=data).json()
        self.assertTrue(repeated['withdrawn']);self.assertEqual(repeated['withdrawal_reason'],'isolated withdrawal')

    def test_invalid_and_oversize_artifacts_do_not_enter_catalog(self):
        for data,status in ((b'not executable',422),(b'x'*(0x300000+1),413),(fake_image().replace(b'fleet_agent',b'other_agent'),422)):
            self.assertEqual(self.client.post('/api/workspace/artifacts',content=data).status_code,status)
        self.assertEqual(TestClient(server.app).get('/api/workspace/artifacts').status_code,401)

    def test_plan_does_not_create_context_and_weak_confirmation_is_rejected(self):
        with server.db() as c:before=c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
        plan=self.client.post('/api/workspace/operation-plan',json=self.request).json()
        self.assertEqual(plan['status'],'supported');self.assertTrue(plan['submission_available']);self.assertFalse(plan['reserved'])
        with server.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],before)
        strict=copy.deepcopy(self.request);strict['params']['confirmation']='boot_version'
        response=self.client.post('/api/workspace/operations',json=strict)
        self.assertEqual(response.status_code,422)
        operation=self.submit();self.assertIsNone(operation['session_id'])
        with server.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM debug_sessions WHERE device_id=?',(self.did,)).fetchone()[0],0)

    def test_success_requires_matching_job_new_boot_and_exact_version(self):
        operation,command=self.dispatch()
        self.assertEqual(command['sha256'],self.image['sha256']);self.assertEqual(command['id'],node_updates_job(self.client,operation['id']))
        self.assertEqual(self.report(command).status_code,409)
        self.assertEqual(self.report(command,boot='new-boot',version='wrong').status_code,409)
        wrong=self.report(dict(command,id='0'*24),boot='new-boot')
        wrong.raise_for_status();self.assertIsNone(wrong.json()['report_ack'])
        response=self.report(command,boot='new-boot');response.raise_for_status();self.assertEqual(response.json()['report_ack'],command['id'])
        result=self.get(operation['id'])
        self.assertEqual(result['outcome'],'succeeded');self.assertEqual(result['resource_status'],'released')
        self.assertTrue(result['result']['runtime_image_identity_verified'])
        self.assertEqual(result['result']['observed_boot_id'],'new-boot')
        self.assertEqual(result['compatibility']['level'],'agent_image_identity')
        schema=json.loads((Path(server.ROOT).parent/'contracts/compat/uart-operation-v0/result.schema.json').read_text())
        Draft202012Validator(schema).validate(result)

    def test_same_key_retry_and_conflict_do_not_create_second_job(self):
        operation=self.submit();self.assertEqual(self.submit()['id'],operation['id'])
        command=self.poll().json()['command'];self.assertIsNotNone(command)
        self.assertEqual(self.submit()['id'],operation['id']);self.assertIsNone(self.poll().json()['command'])
        changed=copy.deepcopy(self.request);changed['timing']['execution_timeout_ms']=10000
        self.assertEqual(self.client.post('/api/workspace/operations',json=changed).status_code,409)
        with server.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs WHERE device_id=?',(self.did,)).fetchone()[0],1)

    def test_dispatch_response_loss_and_independent_process_never_republish(self):
        operation,command=self.dispatch()
        with server.db() as c:c.execute('UPDATE jobs SET updated=? WHERE id=?',(time.time()-100,command['id']))
        self.assertIsNone(self.poll().json()['command'])
        code="""import json;from fastapi.testclient import TestClient;from edbg_server import app as s
with s.db() as c:
 n=c.execute('SELECT * FROM devices WHERE id=?',(__import__('sys').argv[1],)).fetchone()
 client=TestClient(s.app)
 body={'boot_id':'test-boot','version':'3.5.0','partition':'ota_0','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40}
 body['runtime']=json.loads(n['telemetry'])['runtime']
 r=client.post('/api/device/'+n['id']+'/poll',json=body,headers={'Authorization':'Bearer '+__import__('sys').argv[2]})
 assert r.status_code==200,r.text
 assert r.json()['command'] is None
print('no republish')"""
        env=os.environ.copy();env['FLEET_STATE']=str(server.STATE)
        result=subprocess.run([sys.executable,'-c',code,self.did,self.token],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_timeout_holds_resources_and_late_success_reconciles_original(self):
        operation,command=self.dispatch()
        with server.db() as c:c.execute('UPDATE workspace_operations SET execution_deadline=? WHERE id=?',(time.time()-1,operation['id']))
        unknown=self.get(operation['id']);self.assertEqual(unknown['outcome'],'unknown');self.assertEqual(unknown['resource_status'],'unconfirmed')
        self.assertEqual(self.client.post('/api/debug/sessions',json={'device_id':self.did}).status_code,409)
        self.assertEqual(self.client.post('/api/jobs',json={'device_ids':[self.did],'kind':'restart'}).status_code,409)
        another=copy.deepcopy(self.request);another['idempotency_key']=secrets.token_hex(16)
        self.assertEqual(self.client.post('/api/workspace/operations',json=another).status_code,409)
        self.report(command,boot='later-boot').raise_for_status()
        result=self.get(operation['id']);self.assertEqual(result['outcome'],'succeeded');self.assertEqual(result['resource_status'],'released')
        self.assertGreater(result['revision'],unknown['revision'])

    def test_progress_does_not_reopen_timed_out_result_and_terminal_report_wins(self):
        operation,command=self.dispatch()
        with server.db() as c:c.execute('UPDATE workspace_operations SET execution_deadline=? WHERE id=?',(time.time()-1,operation['id']))
        self.get(operation['id']);self.report(command,state='running').raise_for_status()
        self.assertEqual(self.get(operation['id'])['outcome'],'unknown')
        self.report(command,state='failed').raise_for_status();failed=self.get(operation['id'])
        self.assertEqual(failed['outcome'],'failed');self.assertEqual(failed['resource_status'],'released')
        response=self.report(command,boot='a-later-boot');response.raise_for_status()
        self.assertEqual(response.json()['report_ack'],command['id'])
        self.assertEqual(self.get(operation['id'])['outcome'],'failed')

    def test_cancel_before_publish_and_legacy_cancel_cannot_bypass(self):
        operation=self.submit();jid=node_updates_job(self.client,operation['id'])
        self.assertEqual(self.client.post('/api/jobs/'+jid+'/cancel').status_code,409)
        response=self.client.post('/api/workspace/operations/'+operation['id']+'/cancel');response.raise_for_status()
        self.assertEqual(response.json()['outcome'],'cancelled');self.assertIsNone(self.poll().json()['command'])
        self.assertEqual(self.submit()['id'],operation['id'])

    def test_published_cancel_is_explicitly_unsupported(self):
        operation,command=self.dispatch()
        response=self.client.post('/api/workspace/operations/'+operation['id']+'/cancel')
        self.assertEqual(response.status_code,422);self.assertEqual(response.json()['detail']['code'],'CANCELLATION_UNSUPPORTED')
        self.assertFalse(self.get(operation['id'])['cancel_requested'])
        self.assertIsNone(self.poll().json()['command'])

    def test_withdrawal_between_accept_and_publish_stops_dispatch(self):
        operation=self.submit()
        self.client.post('/api/firmware/'+self.image['id']+'/withdraw',json={'reason':'stop before publish'}).raise_for_status()
        self.assertIsNone(self.poll().json()['command'])
        result=self.get(operation['id']);self.assertEqual(result['result']['code'],'ARTIFACT_WITHDRAWN');self.assertEqual(result['resource_status'],'released')

    def test_corrupt_file_digest_mismatch_and_missing_file_are_rejected(self):
        bad=copy.deepcopy(self.request);bad['params']['image']['sha256']='f'*64
        self.assertEqual(self.client.post('/api/workspace/operations',json=bad).status_code,409)
        file=artifacts.path(self.image['id']);data=file.read_bytes();file.write_bytes(b'x'*len(data))
        self.assertEqual(self.client.post('/api/workspace/operations',json=self.request).status_code,409)
        self.assertEqual(self.client.post('/api/workspace/artifacts',content=data).status_code,409)
        file.write_bytes(data);operation=self.submit();file.unlink()
        self.assertIsNone(self.poll().json()['command'])
        self.assertEqual(self.get(operation['id'])['result']['code'],'ARTIFACT_INTEGRITY_FAILED')

    def test_boot_change_and_dispatch_expiry_do_not_send_ota(self):
        operation=self.submit();self.assertIsNone(self.poll(boot='unrelated-reboot').json()['command'])
        self.assertEqual(self.get(operation['id'])['result']['code'],'BOOT_CHANGED_BEFORE_DISPATCH')
        another=copy.deepcopy(self.request);another['idempotency_key']=secrets.token_hex(16)
        operation=self.submit(another)
        with server.db() as c:c.execute('UPDATE workspace_operations SET dispatch_before=? WHERE id=?',(time.time()-1,operation['id']))
        self.assertIsNone(self.poll(boot='unrelated-reboot').json()['command']);self.assertEqual(self.get(operation['id'])['outcome'],'expired')

    def test_legacy_download_authorization_and_integrity_check(self):
        headers={'Authorization':'Bearer '+self.token};url='/api/device/'+self.did+'/firmware/'+self.image['id']
        self.assertEqual(self.client.get(url,headers=headers).status_code,403)
        operation,command=self.dispatch()
        self.assertEqual(self.client.get(url,headers=headers).status_code,200)
        file=artifacts.path(self.image['id']);file.write_bytes(b'corrupted')
        self.assertEqual(self.client.get(url,headers=headers).status_code,409)

    def test_existing_debug_context_and_concurrent_updates_are_exclusive(self):
        self.client.post('/api/debug/sessions',json={'device_id':self.did}).raise_for_status()
        self.assertEqual(self.client.post('/api/workspace/operations',json=self.request).status_code,409)
        with server.db() as c:c.execute("UPDATE debug_sessions SET state='closed' WHERE device_id=?",(self.did,))
        def accept(key):
            request=copy.deepcopy(self.request);request['idempotency_key']=key
            with TestClient(server.app) as client:
                client.headers.update(self.client.headers);client.cookies.update(self.client.cookies)
                return client.post('/api/workspace/operations',json=request).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(accept,[secrets.token_hex(16),secrets.token_hex(16)])),[200,409])

    def test_revocation_after_publication_does_not_prove_completion(self):
        operation,command=self.dispatch()
        self.client.post('/api/devices/'+self.did+'/revoke').raise_for_status()
        result=self.get(operation['id']);self.assertEqual(result['resource_status'],'held')
        with server.db() as c:
            self.assertEqual(c.execute('SELECT held FROM workspace_update_operations WHERE operation_id=?',(operation['id'],)).fetchone()[0],1)
            self.assertEqual(c.execute('SELECT state FROM jobs WHERE id=?',(command['id'],)).fetchone()[0],'dispatched')

    def test_acceptance_failure_rolls_back_job_operation_and_guard(self):
        with server.db() as c:
            c.execute("CREATE TRIGGER fail_update_guard BEFORE INSERT ON workspace_update_operations BEGIN SELECT RAISE(ABORT,'injected'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):self.submit()
        finally:
            with server.db() as c:c.execute('DROP TRIGGER fail_update_guard')
        with server.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM jobs WHERE device_id=?',(self.did,)).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_operations WHERE node_id=?',(self.did,)).fetchone()[0],0)

    def test_artifact_store_always_matches_application_state_and_legacy_upload(self):
        self.assertEqual(artifacts.STATE,server.STATE)
        self.assertEqual(artifacts.path(self.image['id']).parent,server.STATE/'firmware')
        image=fake_image(payload=(self.did+'legacy').encode())
        response=self.client.post('/api/firmware',content=image);response.raise_for_status()
        row=response.json();self.assertEqual(self.client.get('/api/workspace/artifacts/'+row['id']).json()['sha256'],row['sha256'])
        self.assertTrue((server.STATE/'firmware'/(row['id']+'.bin')).exists())

    def test_cli_and_sdk_artifact_import_and_operation_wait_use_http(self):
        factory=lambda url,**kw:Client(url,http_client=self.client,**kw)
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'simulated.bin';image.write_bytes(fake_image(payload=self.did.encode()))
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                code=main(['artifact','import','--image',str(image),'--json','--url','http://testserver'],client_factory=factory)
            self.assertEqual(code,0);self.assertEqual(json.loads(output.getvalue())['data']['id'],self.image['id'])
            with factory('http://testserver') as client:
                self.assertEqual(client.artifact(self.image['id'])['sha256'],self.image['sha256'])
                operation=client.submit_operation(self.request)
                result=client.wait_operation(operation['id'],timeout=.01,interval=.005,initial=operation)
                self.assertTrue(result['wait_timed_out']);self.assertEqual(result['id'],operation['id'])
                self.assertFalse(result['cancel_requested'])


def node_updates_job(client,identity):
    with server.db() as c:return c.execute('SELECT job_id FROM workspace_update_operations WHERE operation_id=?',(identity,)).fetchone()[0]


class UpdateMigrationTests(unittest.TestCase):
    def populated_v5(self,directory):
        path=Path(directory)/'legacy.sqlite3'
        with contextlib.closing(sqlite3.connect(path)) as c, c:
            for name in ('devices','jobs','debug_sessions','debug_tx','i2c_runs'):
                c.execute('CREATE TABLE '+name+' (id TEXT PRIMARY KEY)')
            c.execute("INSERT INTO devices VALUES('n')");c.execute("INSERT INTO debug_sessions VALUES('s')")
        with patch.object(migrations,'MIGRATIONS',migrations.MIGRATIONS[:5]):migrations.migrate(path)
        with contextlib.closing(sqlite3.connect(path)) as c, c:
            c.execute("""INSERT INTO workspace_operations(id,principal,idempotency_key,request,request_digest,action,node_id,binding,resources,session_id,state,resource_status,created,updated,dispatch_before,execution_ms)
             VALUES('o','p','key','{}','digest','i2c.transfer','n','{}','[]','s','accepted','held',1,1,10,1000)""")
            c.execute("INSERT INTO workspace_operation_events VALUES('o',1,1,'{}')")
            c.execute("INSERT INTO i2c_runs VALUES('r','o')")
            c.execute("INSERT INTO workspace_i2c_operations(operation_id,session_id,run_id,bus_key) VALUES('o','s','r','bus')")
            before=c.execute('SELECT * FROM workspace_operations').fetchall()
        return path,before

    def test_populated_migration_preserves_children_foreign_keys_and_indices(self):
        with tempfile.TemporaryDirectory() as directory:
            path,before=self.populated_v5(directory);migrations.migrate(path)
            with contextlib.closing(sqlite3.connect(path)) as c, c:
                self.assertEqual(c.execute('SELECT * FROM workspace_operations').fetchall(),before)
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_operation_events').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_i2c_operations').fetchone()[0],1)
                self.assertEqual(c.execute("PRAGMA foreign_key_list(workspace_operation_events)").fetchone()[2],'workspace_operations')
                c.execute('PRAGMA foreign_keys=ON')
                with self.assertRaises(sqlite3.IntegrityError):c.execute("INSERT INTO workspace_operation_events VALUES('missing',1,1,'{}')")
                indices={r[1] for r in c.execute('PRAGMA index_list(workspace_operations)')}
                self.assertIn('workspace_operation_tx',indices);self.assertIn('workspace_operation_pending',indices)

    def test_failure_after_rebuild_rolls_back_original_table_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path,before=self.populated_v5(directory)
            version,name,sql=migrations.MIGRATIONS[-1]
            with patch.object(migrations,'MIGRATIONS',migrations.MIGRATIONS[:-1]+[(version,name,(*sql,'INVALID SQL'))]):
                with self.assertRaises(sqlite3.Error):migrations.migrate(path)
            with contextlib.closing(sqlite3.connect(path)) as c, c:
                self.assertEqual(c.execute('SELECT * FROM workspace_operations').fetchall(),before)
                self.assertEqual(c.execute('SELECT MAX(version) FROM workspace_migrations').fetchone()[0],5)
                self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

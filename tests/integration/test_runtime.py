import copy
import hashlib
import json
import struct
import time
import unittest
from pathlib import Path

from . import agent_runtime
from . import test_workspace as workspace
from . import test_operations as uart
from . import test_node_updates as updates

server=workspace.server


class RuntimePlanningTests(unittest.TestCase):
    def setUp(self):
        self.fixture=workspace.WorkspaceTests(methodName='runTest');self.fixture.setUp()

    def tearDown(self):self.fixture.tearDown()

    def set_runtime(self,report,build='3.5.0'):
        with server.db() as c:
            c.execute('UPDATE devices SET version=?,telemetry=? WHERE id=?',(build,json.dumps({'runtime':report,'capabilities':['debug_uart_v1','i2c_master_v2']}),self.fixture.did))

    def test_build_version_is_descriptive_and_not_the_capability_gate(self):
        report=agent_runtime('nightly-2030');self.set_runtime(report,'nightly-2030')
        result=self.fixture.plan();self.assertEqual(result['status'],'supported')
        self.assertEqual(result['binding']['agent']['protocol'],'edbg.agent/v1')
        self.assertEqual(result['binding']['agent']['image']['sha256'],'a'*64)

    def test_missing_unknown_protocol_or_wrong_family_never_bypass_provider_checks(self):
        for report in (None,{**agent_runtime(),'protocol':'edbg.agent/v99'},{**agent_runtime(),'family':'pico2w'}):
            self.set_runtime(report);result=self.fixture.plan()
            self.assertNotEqual(result['status'],'supported')
            self.assertIn('AGENT_PROTOCOL_UNSUPPORTED',{r['code'] for r in result['reasons']})

    def test_pending_health_bad_build_and_missing_feature_block_actions(self):
        reports=[agent_runtime(health='pending'),agent_runtime(health='error'),{**agent_runtime(),'image':None},
                 agent_runtime('different-build'),{**agent_runtime(),'features':['runtime.image/1','node.update/1']}]
        for report in reports:
            self.set_runtime(report);self.assertNotEqual(self.fixture.plan()['status'],'supported')

    def test_malformed_poll_does_not_replace_last_accepted_runtime(self):
        from edbg_server import runtime
        with server.db() as c:
            node=c.execute('SELECT * FROM devices WHERE id=?',(self.fixture.did,)).fetchone()
            original=runtime.snapshot(node)
        body={'boot_id':'test-boot','version':'3.5.0','partition':'app0','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40}
        reports=[{**agent_runtime(),'features':['runtime.image/1','runtime.image/1']},
                 {**agent_runtime(),'build':'different'},
                 {**agent_runtime(),'features':['x'*100]},
                 {**agent_runtime(),'uptime_ms':True}]
        token='runtime-test-token'
        with server.db() as c:c.execute('UPDATE devices SET token_hash=? WHERE id=?',(server.digest(token),self.fixture.did))
        for report in reports:
            response=self.fixture.client.post('/api/device/'+self.fixture.did+'/poll',json={**body,'runtime':report},headers={'Authorization':'Bearer '+token})
            self.assertEqual(response.status_code,422)
        with server.db() as c:self.assertEqual(runtime.snapshot(c.execute('SELECT * FROM devices WHERE id=?',(self.fixture.did,)).fetchone()),original)

    def test_running_image_change_within_boot_prevents_queued_context_dispatch(self):
        operation=uart.OperationTests(methodName='runTest');operation.setUp()
        try:
            accepted=operation.submit()
            with server.db() as c:
                report=agent_runtime();report['image']['sha256']='b'*64
                c.execute('UPDATE devices SET telemetry=? WHERE id=?',(json.dumps({'runtime':report,'capabilities':['debug_uart_v1']}),operation.did))
                server.operations.before_poll(c,operation.did,time.time())
            result=operation.get(accepted['id'])
            self.assertEqual(result['outcome'],'failed');self.assertEqual(result['result']['code'],'CAPABILITY_CHANGED_BEFORE_DISPATCH')
            self.assertEqual(result['resource_status'],'released')
        finally:operation.tearDown()


class RuntimeUpdateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=updates.NodeUpdateTests(methodName='runTest');self.fixture.setUp()

    def tearDown(self):self.fixture.tearDown()

    def report(self,command,report):
        body={'boot_id':'measured-new-boot','version':'3.5.0','partition':'app1','uptime':3,'heap':100000,'min_heap':90000,'rssi':-40,
              'report':{'id':command['id'],'state':'succeeded','progress':100,'detail':'health passed'},'runtime':report}
        return self.fixture.client.post('/api/device/'+self.fixture.did+'/poll',json=body,headers={'Authorization':'Bearer '+self.fixture.token})

    def expected(self):
        image=self.fixture.image
        return agent_runtime(image={'algorithm':'sha256-file-v1','sha256':image['sha256'],'size':image['size'],'project':'fleet_agent'})

    def test_same_version_with_wrong_measured_bytes_is_failed_and_never_success(self):
        accepted,command=self.fixture.dispatch();report=self.expected();report['image']['sha256']='c'*64
        response=self.report(command,report);response.raise_for_status();self.assertEqual(response.json()['report_ack'],command['id'])
        result=self.fixture.get(accepted['id']);self.assertEqual(result['outcome'],'failed')
        self.assertEqual(result['result']['code'],'UPDATE_IMAGE_MISMATCH');self.assertFalse(result['result']['runtime_image_identity_verified'])
        self.assertEqual(result['resource_status'],'released')
        self.report(command,self.expected()).raise_for_status();self.assertEqual(self.fixture.get(accepted['id'])['outcome'],'failed')

    def test_matching_digest_with_different_length_is_not_identity_confirmation(self):
        accepted,command=self.fixture.dispatch();report=self.expected();report['image']['size']+=16
        self.report(command,report).raise_for_status()
        self.assertEqual(self.fixture.get(accepted['id'])['result']['code'],'UPDATE_IMAGE_MISMATCH')

    def test_missing_or_pending_evidence_cannot_downgrade_and_late_proof_is_persisted(self):
        accepted,command=self.fixture.dispatch()
        for report in (None,{**self.expected(),'image':None},{**self.expected(),'health':'pending'}):
            self.assertEqual(self.report(command,report).status_code,409)
        self.assertEqual(self.fixture.get(accepted['id'])['state'],'dispatched')
        self.report(command,self.expected()).raise_for_status();result=self.fixture.get(accepted['id'])
        self.assertTrue(result['result']['runtime_image_identity_verified'])
        self.assertEqual(result['result']['observed_runtime']['image']['sha256'],self.fixture.image['sha256'])
        self.assertEqual(result['result']['verification'],'node_flash_sha256')


class ImageStructureTests(unittest.TestCase):
    def test_truncation_overflow_trailers_checksum_and_digest_are_rejected(self):
        from fastapi import HTTPException
        from edbg_server.artifacts import image_metadata
        valid=updates.fake_image();cases=[valid[:-1],valid+b'\0',valid[:23],valid[:40]]
        changed=bytearray(valid);struct.pack_into('<I',changed,28,0xffffffff);cases.append(changed)
        changed=bytearray(valid);changed[-33]^=1;cases.append(changed)
        changed=bytearray(valid);changed[-1]^=1;cases.append(changed)
        changed=bytearray(valid);changed[23]=0;cases.append(changed)
        for data in cases:
            with self.assertRaises(HTTPException):image_metadata(data)
        self.assertEqual(image_metadata(valid),'3.5.0')

    def test_real_built_image_is_accepted_with_its_full_file_digest(self):
        from edbg_server.artifacts import image_metadata
        image=Path(server.ROOT)/'artifacts/4.0.0/fleet_agent.bin'
        if not image.is_file():
            self.skipTest('Build firmware 4.0.0 with fleet/tools/build.ps1 to validate the real image.')
        data=image.read_bytes();self.assertEqual(image_metadata(data),'4.0.0')
        self.assertNotEqual(hashlib.sha256(data).digest(),data[-32:])

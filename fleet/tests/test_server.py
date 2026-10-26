import json
import os
import tempfile
import time
import unittest
from pathlib import Path

temporary = tempfile.TemporaryDirectory()
os.environ['FLEET_STATE'] = temporary.name
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from fastapi.testclient import TestClient


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.client.headers['Origin'] = 'http://testserver'
        self.client.post('/api/login', json=server.ADMIN).raise_for_status()
        self.device_id = 's3-' + os.urandom(6).hex()
        result = self.client.post('/api/devices', json={'id': self.device_id, 'name': 'Test'}).json()
        self.token = result['token']
        self.poll = {'boot_id': 'boot-a', 'version': '3.0.0', 'partition': 'app0', 'uptime': 3, 'heap': 100000, 'min_heap': 90000, 'rssi': -50}

    def test_web_scripts_are_served(self):
        import re
        html = self.client.get('/').text
        sources = re.findall(r'<script src="([^"]+)"', html)
        self.assertIn('/debug-model.js', sources)
        for source in sources:
            response = self.client.get(source)
            self.assertEqual(response.status_code, 200, source)
            self.assertIn('javascript', response.headers['content-type'], source)

    def device_poll(self, body=None, token=None, device_id=None):
        return self.client.post('/api/device/' + (device_id or self.device_id) + '/poll', json=body or self.poll, headers={'Authorization': 'Bearer ' + (token or self.token)})

    def firmware(self):
        import hashlib,struct
        from functools import reduce
        from operator import xor
        image = bytearray(256)
        image[0] = 0xE9
        image[12:14] = b'\x09\x00'
        image[32:36] = bytes.fromhex('3254cdab')
        image[48:53] = b'3.1.0'
        image[80:91] = b'fleet_agent'
        # Different fixture image per test, keeping version fixed.
        image[-8:] = os.urandom(8)
        segment=image[32:]+bytes(32)
        header=bytearray(24);header[0]=0xe9;header[1]=1;header[12]=9;header[23]=1
        image=header+struct.pack('<II',0x3c000020,len(segment))+segment
        image.extend(b'\0'*(-(len(image)+1)%16));image.append(reduce(xor,segment,0xef))
        image.extend(hashlib.sha256(image).digest())
        result = self.client.post('/api/firmware', content=bytes(image), headers={'Content-Type': 'application/octet-stream'})
        result.raise_for_status()
        return result.json()['id']

    def test_auth_and_isolation(self):
        self.assertEqual(TestClient(server.app).get('/api/devices').status_code, 401)
        self.assertEqual(self.device_poll(token='wrong').status_code, 401)
        other = self.client.post('/api/devices', json={'id': 's3-' + os.urandom(6).hex(), 'name': 'Other'}).json()
        self.assertEqual(self.device_poll(device_id=other['id']).status_code, 401)
        self.assertEqual(self.device_poll().status_code, 200)

    def test_origin_required(self):
        self.assertEqual(self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'restart'}, headers={'Origin': 'https://attacker.invalid'}).status_code, 403)

    def test_batch_atomic_and_conflict(self):
        response = self.client.post('/api/jobs', json={'device_ids': [self.device_id, 'nonexistent'], 'kind': 'restart'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len([j for j in self.client.get('/api/jobs').json() if j['device_id'] == self.device_id]), 0)
        self.assertEqual(self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'restart'}).status_code, 200)
        self.assertEqual(self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'restart'}).status_code, 409)

    def test_ota_cannot_bypass_managed_image_confirmation(self):
        firmware_id = self.firmware()
        self.assertEqual(self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'ota', 'firmware_id': firmware_id}).status_code,422)
        self.assertIsNone(self.device_poll().json()['command'])

    def test_log_replay_deduplicated(self):
        body = {**self.poll, 'logs': [{'seq': 1, 'line': '<script>untrusted</script>'}]}
        self.device_poll(body)
        self.device_poll(body)
        self.assertEqual(len(self.client.get(f'/api/devices/{self.device_id}/logs').json()), 1)

    def test_firmware_and_download_permissions(self):
        self.assertEqual(self.client.post('/api/firmware', content=b'bad').status_code, 400)
        firmware_id = self.firmware()
        self.assertEqual(self.client.get(f'/api/device/{self.device_id}/firmware/{firmware_id}', headers={'Authorization': 'Bearer ' + self.token}).status_code, 403)
        self.assertEqual(self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'ota', 'firmware_id': firmware_id}).status_code,422)
        self.device_poll()
        self.assertEqual(self.client.get(f'/api/device/{self.device_id}/firmware/{firmware_id}', headers={'Authorization': 'Bearer ' + self.token}).status_code, 403)

    def test_withdrawn_firmware_is_retained_but_cannot_be_deployed(self):
        fid = self.firmware()
        self.client.post(f'/api/firmware/{fid}/withdraw',json={'reason':'Failed bench validation'}).raise_for_status()
        self.assertEqual(next(f for f in self.client.get('/api/firmware').json() if f['id']==fid)['withdrawn'],'Failed bench validation')
        self.assertEqual(self.client.post('/api/jobs', json={'device_ids':[self.device_id],'kind':'ota','firmware_id':fid}).status_code,422)
        self.assertEqual(self.client.get(f'/api/device/{self.device_id}/firmware/{fid}',headers={'Authorization':'Bearer '+self.token}).status_code,403)

    def test_cancel_only_queued_and_revoke(self):
        job = self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'restart'}).json()['job_ids'][0]
        self.assertEqual(self.client.post(f'/api/jobs/{job}/cancel').status_code, 200)
        job = self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'restart'}).json()['job_ids'][0]
        self.device_poll()
        self.assertEqual(self.client.post(f'/api/jobs/{job}/cancel').status_code, 409)
        self.client.post(f'/api/devices/{self.device_id}/revoke')
        self.assertEqual(self.device_poll().status_code, 401)

    def test_multiple_devices_receive_separate_batch_jobs(self):
        nodes = []
        for index in range(8):
            result = self.client.post('/api/devices', json={'id': 's3-' + os.urandom(6).hex(), 'name': f'Batch {index}', 'group': 'Batch'}).json()
            nodes.append(result)
        jobs = self.client.post('/api/jobs', json={'device_ids': [d['id'] for d in nodes], 'kind': 'diagnostics'}).json()['job_ids']
        self.assertEqual(len(set(jobs)), 8)
        received = set()
        for node in nodes:
            result = self.device_poll(device_id=node['id'], token=node['token']).json()
            command = result['command']
            self.assertIsNotNone(command)
            received.add(command['id'])
            body = {**self.poll, 'report': {'id': command['id'], 'state': 'succeeded', 'detail': 'simulated test result'}}
            self.assertEqual(self.device_poll(body, device_id=node['id'], token=node['token']).json()['report_ack'], command['id'])
        self.assertEqual(received, set(jobs))

    def test_device_bearer_does_not_grant_operator_access(self):
        other = TestClient(server.app)
        other.headers['Authorization'] = 'Bearer ' + self.token
        self.assertEqual(other.get('/api/devices').status_code, 401)
        self.assertEqual(other.get('/api/jobs').status_code, 401)

    def test_recent_logs_and_boot_filter_preserve_incremental_cursor(self):
        self.device_poll({**self.poll, 'boot_id': 'older-boot', 'logs': [{'seq': 1, 'line': 'old line'}]})
        self.device_poll({**self.poll, 'logs': [{'seq': n, 'line': f'line {n}'} for n in range(1, 6)]})
        path = f'/api/devices/{self.device_id}/logs'
        response = self.client.get(path, params={'tail': 'true', 'boot_id': 'boot-a', 'limit': 2})
        rows = response.json()
        self.assertEqual([r['line'] for r in rows], ['line 4', 'line 5'])
        self.device_poll({**self.poll, 'logs': [{'seq': 6, 'line': 'line 6'}]})
        rows = self.client.get(path, params={'after': rows[-1]['id'], 'boot_id': 'boot-a'}).json()
        self.assertEqual([r['line'] for r in rows], ['line 6'])
        self.assertEqual(self.client.get(path, params={'limit': 1001}).status_code, 422)
        self.assertEqual(TestClient(server.app).get(path, params={'tail': 'true'}).status_code, 401)

    def test_summary_counts_active_jobs_and_requires_operator_session(self):
        before = self.client.get('/api/summary').json()['active_jobs']
        self.client.post('/api/jobs', json={'device_ids': [self.device_id], 'kind': 'diagnostics'})
        self.assertEqual(self.client.get('/api/summary').json()['active_jobs'], before + 1)
        self.assertEqual(TestClient(server.app).get('/api/summary').status_code, 401)


if __name__ == '__main__':
    unittest.main(verbosity=2)

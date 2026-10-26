import secrets
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import server, TestClient
import debug_api


class DebugTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.client.headers['Origin'] = 'http://testserver'
        self.client.post('/api/login', json=server.ADMIN).raise_for_status()
        self.id = 's3-' + secrets.token_hex(6)
        self.token = self.client.post('/api/devices', json={'id': self.id, 'name': 'Bench'}).json()['token']
        debug_api.BENCH[self.id] = 'target'
        self.body = {'boot_id':'boot-1','version':'3.2.0','partition':'app0','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1']}
        self.poll()

    def tearDown(self):
        debug_api.BENCH.pop(self.id)

    def poll(self, **values):
        return self.client.post(f'/api/device/{self.id}/poll', json={**self.body, **values}, headers={'Authorization':'Bearer '+self.token})

    def opened(self):
        result=self.client.post('/api/debug/sessions', json={'device_id':self.id})
        result.raise_for_status()
        sid=result.json()['id']
        control=self.poll(debug={'state':'idle'}).json()['debug_control']
        self.assertEqual(control['session'],sid)
        self.assertEqual(control['role'],'target')
        self.assertLessEqual(control['lease_ms'],10000)
        self.poll(debug={'session':sid,'state':'active'})
        return sid

    def row(self, sid):
        return next(s for s in self.client.get('/api/debug').json()['sessions'] if s['id']==sid)

    def test_capability_freshness_auth_and_bench_guards(self):
        unauth=TestClient(server.app)
        self.assertEqual(unauth.get('/api/debug').status_code,401)
        self.assertEqual(unauth.get('/api/debug/sessions/any/rx').status_code,401)
        self.assertEqual(unauth.post('/api/debug/sessions',json={'device_id':self.id},headers={'Origin':'http://testserver'}).status_code,401)
        self.poll(capabilities=[])
        self.assertEqual(self.client.post('/api/debug/sessions',json={'device_id':self.id}).status_code,409)
        self.poll()
        with server.db() as c:c.execute('UPDATE devices SET last_seen=? WHERE id=?',(time.time()-30,self.id))
        self.assertEqual(self.client.post('/api/debug/sessions',json={'device_id':self.id}).status_code,409)
        self.assertEqual(self.client.post('/api/debug/sessions',json={'device_id':'s3-000000000000'}).status_code,400)

    def test_profile_contract_describes_interfaces_without_exposing_control_credentials(self):
        overview=self.client.get('/api/debug').json()
        profile=overview['profiles'][0]
        self.assertIn({'device_id':self.id,'role':'target'},profile['members'])
        self.assertEqual(profile['uart']['baud'],115200)
        self.assertEqual(profile['i2c']['mode'],'paired_responder')
        self.assertEqual(profile['i2c']['max_bytes'],64)
        self.assertNotIn('owner',profile)
        self.assertNotIn('token',profile)

    def test_lifecycle_interlocks_both_directions(self):
        sid=self.opened()
        self.assertEqual(self.client.post('/api/debug/sessions',json={'device_id':self.id}).status_code,409)
        self.assertEqual(self.client.post('/api/jobs',json={'device_ids':[self.id],'kind':'restart'}).status_code,409)
        self.client.post(f'/api/debug/sessions/{sid}/close').raise_for_status()
        self.assertIsNone(self.poll(debug={'session':sid,'state':'active'}).json()['debug_control'])
        self.assertEqual(self.row(sid)['state'],'closing')
        self.poll(debug={'state':'idle'})
        self.assertEqual(self.row(sid)['state'],'closed')
        self.assertEqual(self.row(sid)['snapshot']['state'],'idle')
        self.client.post('/api/jobs',json={'device_ids':[self.id],'kind':'diagnostics'}).raise_for_status()
        self.assertEqual(self.client.post('/api/debug/sessions',json={'device_id':self.id}).status_code,409)

    def test_send_idempotency_explicit_ack_and_owner(self):
        sid=self.opened();path=f'/api/debug/sessions/{sid}/send'
        data={'data':'0041ff','request_id':'request-000000001'}
        tx=self.client.post(path,json=data).json()['id']
        self.assertEqual(self.client.post(path,json=data).json()['id'],tx)
        self.assertEqual(self.client.post(path,json={**data,'data':'01'}).status_code,409)
        self.assertEqual(self.client.post(path,json={**data,'request_id':'request-000000002'}).status_code,409)
        control=self.poll(debug={'session':sid,'state':'active'}).json()['debug_control']
        self.assertEqual(control['tx'],{'id':tx,'data':'0041ff'})
        # The same ID is returned until acknowledged: firmware must deduplicate it.
        self.assertEqual(self.poll(debug={'session':sid,'state':'active'}).json()['debug_control']['tx']['id'],tx)
        self.poll(debug={'session':sid,'state':'active','tx_id':tx})
        self.assertEqual(self.row(sid)['tx'][0]['state'],'sent')
        other=TestClient(server.app);other.headers['Origin']='http://testserver'
        other.post('/api/login',json=server.ADMIN).raise_for_status()
        self.assertEqual(other.post(path,json=data).status_code,403)

    def test_rx_dedup_boot_scope_and_target_cannot_claim_boot_port(self):
        sid=self.opened()
        snapshot={'session':sid,'state':'active','rx':[{'seq':1,'port':1,'us':10,'data':'003cff'},{'seq':2,'port':2,'us':11,'data':'aa'}],'dropped':3,'uart_errors':1}
        self.poll(debug=snapshot);result=self.poll(debug=snapshot).json()
        rows=self.client.get(f'/api/debug/sessions/{sid}/rx').json()
        self.assertEqual([r['data'] for r in rows],['003cff'])
        self.assertEqual(result['debug_control']['rx_ack'],1)
        self.assertEqual(self.row(sid)['snapshot']['dropped'],3)
        self.assertEqual(self.client.get(f'/api/debug/sessions/{sid}/rx',params={'after':rows[-1]['id']}).json(),[])
        self.assertIsNone(self.poll(boot_id='boot-2',debug={'state':'idle'}).json()['debug_control'])
        self.assertEqual(self.row(sid)['state'],'lost')

    def test_expiry_does_not_queue_or_replay_and_requires_idle_confirmation(self):
        sid=self.opened();path=f'/api/debug/sessions/{sid}/send'
        tx=self.client.post(path,json={'data':'55','request_id':'request-000000001'}).json()['id']
        self.poll(debug={'session':sid,'state':'active'})
        with server.db() as c:c.execute('UPDATE debug_tx SET expires=? WHERE id=?',(time.time()-1,tx))
        self.assertNotIn('tx',self.poll(debug={'session':sid,'state':'active'}).json()['debug_control'])
        self.assertEqual(self.row(sid)['tx'][0]['state'],'unknown')
        with server.db() as c:c.execute('UPDATE debug_sessions SET expires=? WHERE id=?',(time.time()-1,sid))
        self.assertIsNone(self.poll(debug={'session':sid,'state':'active'}).json()['debug_control'])
        self.assertEqual(self.row(sid)['state'],'expired')
        self.assertEqual(self.client.post('/api/jobs',json={'device_ids':[self.id],'kind':'restart'}).status_code,409)
        self.poll(debug={'state':'idle'})
        self.assertEqual(self.row(sid)['state'],'closed')

    def test_local_lease_expiry_closes_and_boot_reboot_cancels_pending_tx(self):
        sid=self.opened()
        self.poll(debug={'session':sid,'state':'expired','error':'Lease expired'})
        self.assertEqual(self.row(sid)['state'],'closed')
        sid=self.opened()
        tx=self.client.post(f'/api/debug/sessions/{sid}/send',json={'data':'55','request_id':'request-000000001'}).json()['id']
        self.poll(boot_id='boot-2',debug={'state':'idle'})
        self.assertEqual(self.row(sid)['state'],'lost')
        self.assertEqual(self.row(sid)['tx'][0]['state'],'unknown')
        self.assertEqual(self.row(sid)['tx'][0]['id'],tx)

    def test_payload_limits_origin_and_cross_session_ack(self):
        sid=self.opened();path=f'/api/debug/sessions/{sid}/send'
        for invalid in ('0','zz','00'*257,''):
            self.assertEqual(self.client.post(path,json={'data':invalid,'request_id':'request-000000001'}).status_code,422)
        self.assertEqual(self.client.post(path,json={'data':'55','request_id':'request-000000001'},headers={'Origin':'https://invalid.example'}).status_code,403)
        tx=self.client.post(path,json={'data':'55','request_id':'request-000000001'}).json()['id']
        self.poll(debug={'session':sid,'state':'active'})
        self.poll(debug={'session':'another-session','state':'active','tx_id':tx})
        self.assertEqual(self.row(sid)['tx'][0]['state'],'dispatched')

    def test_record_snapshot_reports_retention_and_exports_tx_payload(self):
        sid = self.opened()
        now = time.time()
        with server.db() as c:
            c.executemany('INSERT INTO debug_rx(session_id,seq,port,us,data,created) VALUES(?,?,?,?,?,?)',
                          [(sid, n, 1, n * 10, '4142', now + n / 10000) for n in range(1, 2006)])
        self.poll(debug={'session': sid, 'state': 'active', 'dropped':3, 'rx': [{'seq': 2006, 'port': 1, 'us': 20060, 'data': '4344'}]})
        self.client.post(f'/api/debug/sessions/{sid}/send', json={'data':'e4b8ad', 'request_id':'record-test-000001'}).raise_for_status()
        url = f'/api/debug/sessions/{sid}/record'
        first = self.client.get(url, params={'limit':5}).json()
        self.assertTrue(first['has_more'])
        self.assertEqual(first['scope']['retained_frames'], 2000)
        self.assertEqual(first['scope']['unavailable_before'], 6)
        self.assertEqual(first['scope']['gaps_within'], 0)
        self.assertEqual(first['scope']['retained_bytes'], 4000)
        self.assertEqual(first['frames'][0]['seq'], 7)
        second = self.client.get(url, params={'after':first['frames'][-1]['id'], 'limit':5}).json()
        self.assertEqual(second['frames'][0]['seq'], 12)
        exported = self.client.get(url, params={'limit':2000}).json()
        self.assertFalse(exported['has_more'])
        self.assertEqual(len(exported['frames']), exported['scope']['retained_frames'])
        self.assertEqual(exported['transmissions'][0]['data'], 'e4b8ad')
        self.assertNotIn('owner', exported['session'])
        self.client.post(f'/api/debug/sessions/{sid}/close')
        self.poll(debug={'state':'idle'})
        self.assertEqual(self.client.get(url).json()['scope']['device_reported_dropped'], 3)
        self.assertEqual(TestClient(server.app).get(url).status_code, 401)
        self.assertEqual(self.client.get('/api/debug/sessions/missing/record').status_code, 404)

    def test_session_history_is_paginated_and_device_scoped(self):
        for _ in range(3):
            sid = self.opened()
            self.client.post(f'/api/debug/sessions/{sid}/close')
            self.poll(debug={'state':'idle'})
        first = self.client.get('/api/debug/sessions', params={'device_id':self.id, 'limit':2}).json()
        second = self.client.get('/api/debug/sessions', params={'device_id':self.id, 'limit':2, 'before':first['next']}).json()
        self.assertEqual(len(first['sessions']), 2)
        self.assertEqual(len(second['sessions']), 1)
        self.assertIsNone(second['next'])
        self.assertEqual(len({s['id'] for s in first['sessions'] + second['sessions']}), 3)
        self.assertTrue(all(s['device_id'] == self.id and 'owner' not in s for s in first['sessions']))
        self.assertEqual(self.client.get('/api/debug/sessions', params={'device_id':'other','before':first['next']}).status_code, 404)
        self.assertEqual(TestClient(server.app).get('/api/debug/sessions', params={'device_id':self.id}).status_code, 401)


if __name__=='__main__':unittest.main(verbosity=2)

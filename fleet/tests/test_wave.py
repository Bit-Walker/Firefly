import secrets
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import server, TestClient
import debug_api
import wave_api


class WaveTests(unittest.TestCase):
    def setUp(self):
        self.c=TestClient(server.app);self.c.headers['Origin']='http://testserver'
        self.c.post('/api/login',json=server.ADMIN).raise_for_status()
        self.nodes={}
        self.base={'boot_id':'wave-boot','version':'3.3.0','partition':'app0','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1','wave_rmt_v1']}
        for role in ('target','probe'):
            did='s3-'+secrets.token_hex(6)
            token=self.c.post('/api/devices',json={'id':did,'name':role}).json()['token']
            self.nodes[role]={'id':did,'token':token}
            debug_api.BENCH[did]=role
            self.poll(role)
            sid=self.c.post('/api/debug/sessions',json={'device_id':did}).json()['id']
            self.nodes[role]['sid']=sid
            self.poll(role,debug={'session':sid,'state':'active'})
        self.request={'receiver_session':self.nodes['probe']['sid'],'source_session':self.nodes['target']['sid'],'high_us':500,'low_us':500,'count':3,'request_id':secrets.token_hex(12)}

    def tearDown(self):
        for n in self.nodes.values():debug_api.BENCH.pop(n['id'],None)

    def poll(self,role,**kwargs):
        n=self.nodes[role]
        return self.c.post(f"/api/device/{n['id']}/poll",json={**self.base,**kwargs},headers={'Authorization':'Bearer '+n['token']})

    def create(self,**kwargs):
        response=self.c.post('/api/waves',json={**self.request,**kwargs});response.raise_for_status();return response.json()['id']

    def wave(self,role,wid,state,**kwargs):
        return self.poll(role,debug={'session':self.nodes[role]['sid'],'state':'active','wave':{'id':wid,'state':state,'gpio':6,**kwargs}}).json()['debug_control']['wave']

    def row(self,wid):return self.c.get('/api/waves/'+wid).json()

    def test_armed_handshake_measurement_and_request_dedup(self):
        wid=self.create();self.assertEqual(self.create(),wid)
        self.assertIsNone(self.wave('target',wid,'idle'))
        cmd=self.wave('probe',wid,'armed')
        self.assertEqual(cmd,{'id':wid,'kind':'capture'})
        cmd=self.wave('target',wid,'idle')
        self.assertEqual(cmd,{'id':wid,'kind':'pulse','high_us':500,'low_us':500,'count':3})
        self.wave('probe',wid,'captured',segments=[{'level':1,'us':500},{'level':0,'us':500}]*2+[{'level':1,'us':500}])
        self.assertIsNone(self.wave('target',wid,'sent'))
        row=self.row(wid)
        self.assertEqual(row['state'],'completed')
        self.assertEqual(row['stats']['high_pulses'],3)
        self.assertEqual(row['stats']['frequency_hz'],1000)
        self.assertEqual(row['stats']['duty_percent'],50)
        self.assertEqual(row['stats']['low_us_mean'],500)
        self.assertTrue(row['stats']['right_censored'])
        self.assertEqual(self.create(),wid) # A retry after completion cannot emit again.
        self.assertIsNone(self.wave('target',wid,'sent'))

    def test_validation_owner_conflict_and_capabilities(self):
        for bad in ({'count':65},{'high_us':49},{'low_us':10001},{'high_us':10000,'low_us':10000,'count':64},{'source_session':self.request['receiver_session']},{'gpio':4}):
            self.assertEqual(self.c.post('/api/waves',json={**self.request,**bad}).status_code,422)
        other=TestClient(server.app);other.headers['Origin']='http://testserver';other.post('/api/login',json=server.ADMIN)
        self.assertEqual(other.post('/api/waves',json=self.request).status_code,403)
        wid=self.create()
        self.assertEqual(self.c.post('/api/waves',json={**self.request,'request_id':secrets.token_hex(12)}).status_code,409)
        self.assertEqual(self.c.post('/api/waves',json={**self.request,'count':4}).status_code,409)
        self.assertEqual(TestClient(server.app).get('/api/waves/'+wid).status_code,401)

    def test_capture_only_never_returns_output_and_cancel_releases_operation(self):
        wid=self.create(mode='capture',source_session=None)
        self.assertIsNone(self.wave('target',wid,'idle'))
        self.assertEqual(self.wave('probe',wid,'armed')['kind'],'capture')
        self.c.post(f'/api/waves/{wid}/cancel').raise_for_status()
        self.assertIsNone(self.wave('probe',wid,'armed'))
        self.assertEqual(self.row(wid)['state'],'cancelled')
        self.create(request_id=secrets.token_hex(12))

    def test_session_close_cannot_replay_output(self):
        wid=self.create()
        self.c.post(f"/api/debug/sessions/{self.nodes['probe']['sid']}/close")
        self.assertIsNone(self.wave('target',wid,'idle'))
        self.assertEqual(self.row(wid)['state'],'cancelled')

    def test_peer_reboot_stops_run_before_output(self):
        wid=self.create()
        self.wave('probe',wid,'armed')
        response=self.poll('target',boot_id='new-boot',debug={'state':'idle'}).json()
        self.assertIsNone(response['debug_control'])
        self.assertIsNone(self.wave('probe',wid,'armed'))
        self.assertEqual(self.row(wid)['state'],'cancelled')

    def test_wrong_pin_partial_capture_and_idle_time_are_not_success(self):
        wid=self.create()
        self.assertIsNone(self.wave('probe',wid,'armed',gpio=7))
        self.assertEqual(self.row(wid)['state'],'failed')
        wid=self.create(request_id=secrets.token_hex(12))
        self.wave('probe',wid,'armed');self.wave('target',wid,'idle')
        self.wave('probe',wid,'captured',segments=[{'level':1,'us':500},{'level':0,'us':20000}])
        self.wave('target',wid,'sent')
        self.assertEqual(self.row(wid)['state'],'failed')
        self.assertIn('脉冲数',self.row(wid)['detail'])

    def test_expiry_offline_and_no_early_trigger(self):
        wid=self.create()
        self.wave('probe',wid,'captured',segments=[{'level':1,'us':500},{'level':0,'us':20000}])
        self.assertEqual(self.row(wid)['state'],'failed')
        wid=self.create(request_id=secrets.token_hex(12))
        with server.db() as c:c.execute('UPDATE wave_runs SET expires=? WHERE id=?',(time.time()-1,wid))
        self.assertIsNone(self.wave('target',wid,'idle'))
        self.assertEqual(self.row(wid)['state'],'expired')
        self.poll('target',capabilities=['debug_uart_v1'],debug={'session':self.nodes['target']['sid'],'state':'active'})
        self.assertEqual(self.c.post('/api/waves',json={**self.request,'request_id':secrets.token_hex(12)}).status_code,409)

    def test_censored_tail_and_split_levels(self):
        stats=wave_api.analyze([{'level':1,'us':200},{'level':1,'us':300},{'level':0,'us':500},{'level':1,'us':500}])
        self.assertEqual(stats['high_pulses'],2)
        self.assertEqual(stats['high_us_mean'],500)
        self.assertEqual(stats['frequency_hz'],1000)
        self.assertIsNone(wave_api.analyze([{'level':1,'us':20000}])['frequency_hz'])
        self.assertEqual(wave_api.analyze([{'level':1,'us':500}])['high_pulses'],1)

    def test_paired_width_mismatch_is_reported_even_if_pulse_count_matches(self):
        wid=self.create()
        self.wave('probe',wid,'armed');self.wave('target',wid,'idle')
        self.wave('probe',wid,'captured',segments=[{'level':1,'us':500},{'level':0,'us':250}]*2+[{'level':1,'us':500}])
        self.wave('target',wid,'sent')
        self.assertEqual(self.row(wid)['state'],'failed')
        self.assertIn('脉宽',self.row(wid)['detail'])


if __name__=='__main__':unittest.main(verbosity=2)

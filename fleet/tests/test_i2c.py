import secrets
import sys
import time
import unittest
from pathlib import Path
import json
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_server import server, TestClient
import debug_api
import i2c_api


class I2CTests(unittest.TestCase):
    def setUp(self):
        self.c=TestClient(server.app);self.c.headers['Origin']='http://testserver'
        self.c.post('/api/login',json=server.ADMIN).raise_for_status()
        self.nodes={}
        self.base={'boot_id':'i2c-boot','version':'3.5.0','partition':'app1','uptime':1,'heap':100000,'min_heap':90000,'rssi':-40,'capabilities':['debug_uart_v1','wave_rmt_v1','i2c_bench_v1','i2c_master_v2']}
        for role in ('target','probe'):
            did='s3-'+secrets.token_hex(6)
            token=self.c.post('/api/devices',json={'id':did,'name':role}).json()['token']
            self.nodes[role]={'id':did,'token':token};debug_api.BENCH[did]=role
            self.poll(role)
            self.nodes[role]['sid']=self.c.post('/api/debug/sessions',json={'device_id':did}).json()['id']
            self.report(role,'','idle')
        self.body={'master_session':self.nodes['probe']['sid'],'target_session':self.nodes['target']['sid'],'request_id':secrets.token_hex(12)}

    def tearDown(self):
        for n in self.nodes.values():
            self.c.post('/api/debug/sessions/'+n['sid']+'/close')
            debug_api.BENCH.pop(n['id'],None)

    def poll(self,role,**kw):
        n=self.nodes[role]
        r=self.c.post('/api/device/'+n['id']+'/poll',json={**self.base,**kw},headers={'Authorization':'Bearer '+n['token']})
        r.raise_for_status();return r.json()

    def report(self,role,rid,state,**kw):
        response=self.poll(role,debug={'session':self.nodes[role]['sid'],'state':'active','i2c':{'id':rid,'state':state,**kw}})
        return (response.get('debug_control') or {}).get('i2c')

    def create(self,**kw):
        r=self.c.post('/api/i2c',json={**self.body,**kw});r.raise_for_status();return r.json()['id']

    def row(self,rid):return self.c.get('/api/i2c/'+rid).json()

    def test_scan_handshake_and_dedup(self):
        rid=self.create();self.assertEqual(self.create(),rid)
        self.assertIsNone(self.report('probe','','idle'))
        self.assertEqual(self.report('target','','idle')['kind'],'target')
        self.report('target',rid,'ready')
        cmd=self.report('probe','','idle');self.assertEqual(cmd['speed_hz'],10000)
        self.assertEqual(cmd['target_address'],0x42)
        self.assertIsNone(self.report('probe',rid,'done',found=[0x42]))
        self.assertEqual(self.row(rid)['state'],'completed')
        self.assertEqual(self.create(),rid)
        self.assertIsNone(self.report('probe',rid,'done',found=[0x42]))

    def test_write_read_waits_for_target_and_checks_bytes(self):
        rid=self.create(operation='write_read',data='AA01',read_len=3,reply='102030')
        self.report('target',rid,'ready');self.report('probe','','idle')
        self.report('probe',rid,'done',data='102030')
        self.assertEqual(self.row(rid)['state'],'verifying')
        self.report('target',rid,'ready',data='aa01')
        r=self.row(rid);self.assertEqual(r['state'],'completed');self.assertNotIn('owner',r)
        self.assertEqual(r['master']['device_id'],self.nodes['probe']['id'])
        rid=self.create(operation='read',read_len=1,reply='AA',request_id=secrets.token_hex(12))
        self.report('target',rid,'ready');self.report('probe','','idle')
        self.report('probe',rid,'done',data='bb');self.assertEqual(self.row(rid)['state'],'failed')

    def test_validation_permissions_busy_and_capability(self):
        for kw in ({'operation':'write'},{'operation':'read','read_len':65},{'reply':'01'},{'address':0},{'speed_hz':100000},{'data':'xx'}):
            self.assertEqual(self.c.post('/api/i2c',json={**self.body,**kw}).status_code,422)
        self.assertEqual(TestClient(server.app).post('/api/i2c',json=self.body,headers={'Origin':'http://testserver'}).status_code,401)
        other=TestClient(server.app);other.headers['Origin']='http://testserver';other.post('/api/login',json=server.ADMIN)
        self.assertEqual(other.post('/api/i2c',json=self.body).status_code,403)
        rid=self.create()
        self.assertEqual(self.c.post('/api/i2c',json={**self.body,'request_id':secrets.token_hex(12)}).status_code,409)
        self.assertEqual(self.c.post('/api/i2c',json={**self.body,'address':0x43}).status_code,409)
        self.assertEqual(self.c.post('/api/waves',json={'receiver_session':self.nodes['probe']['sid'],'mode':'capture','request_id':secrets.token_hex(12)}).status_code,409)
        self.c.post('/api/i2c/'+rid+'/cancel')
        self.poll('target',capabilities=['debug_uart_v1'],debug={'session':self.nodes['target']['sid'],'state':'active'})
        self.assertEqual(self.c.post('/api/i2c',json={**self.body,'request_id':secrets.token_hex(12)}).status_code,409)

    def test_cancel_and_expiry_never_claim_unconfirmed_write_failed_to_execute(self):
        rid=self.create();self.c.post('/api/i2c/'+rid+'/cancel');self.assertEqual(self.row(rid)['state'],'cancelled')
        rid=self.create(operation='write',data='55',request_id=secrets.token_hex(12))
        self.report('target',rid,'ready');self.report('probe','','idle')
        with server.db() as c:c.execute('UPDATE i2c_runs SET expires=? WHERE id=?',(time.time()-1,rid))
        self.assertIsNone(self.report('probe',rid,'done'))
        self.assertEqual(self.row(rid)['state'],'unknown')
        self.assertIsNone(self.report('target',rid,'ready'))

    def test_peer_reboot_and_nack_stop_run(self):
        rid=self.create();self.report('target',rid,'ready')
        self.poll('target',boot_id='different',debug={'state':'idle'})
        self.assertIsNone(self.report('probe','','idle'))
        self.assertEqual(self.row(rid)['state'],'cancelled')

    def test_nack_is_reported_with_raw_error(self):
        rid=self.create(operation='read',address=0x43,read_len=1,reply='00')
        self.report('target',rid,'ready');self.report('probe','','idle')
        self.report('probe',rid,'failed',error='NACK')
        self.assertEqual(self.row(rid)['state'],'failed')
        self.assertEqual(self.row(rid)['master_report']['error'],'NACK')

    def external(self,**kw):
        return self.create(mode='external',target_session=None,**kw)

    def test_independent_scan_does_not_require_or_create_responder(self):
        self.c.post('/api/debug/sessions/'+self.nodes['target']['sid']+'/close')
        self.poll('target',debug={'state':'idle'})
        rid=self.external(scan_start=0x40,scan_end=0x47)
        cmd=self.report('probe','','idle')
        self.assertEqual(cmd['kind'],'master');self.assertEqual(cmd['mode'],'external')
        self.assertEqual(cmd['scan_start'],0x40)
        self.assertIsNone(self.report('target','','idle'))
        self.report('probe',rid,'done',found=[0x42],sda_idle=1,scl_idle=1)
        row=self.row(rid);self.assertEqual(row['state'],'completed');self.assertIsNone(row['target'])
        self.assertEqual(row['master_report']['sda_idle'],1)

    def test_independent_reads_preserve_actual_bytes_without_fabricated_expected_response(self):
        rid=self.external(operation='write_read',data='10',read_len=3)
        self.report('probe','','idle');self.report('probe',rid,'done',data='beef01')
        row=self.row(rid);self.assertEqual(row['state'],'completed')
        self.assertEqual(row['master_report']['data'],'beef01');self.assertEqual(row['target_report'],{})
        rid=self.external(operation='read',read_len=3,request_id=secrets.token_hex(12))
        self.report('probe','','idle');self.report('probe',rid,'done',data='beef')
        self.assertEqual(self.row(rid)['state'],'failed')

    def test_independent_master_can_use_either_member_but_shared_bus_is_exclusive(self):
        rid=self.external()
        other={**self.body,'mode':'external','target_session':None,'master_session':self.nodes['target']['sid'],'request_id':secrets.token_hex(12)}
        self.assertEqual(self.c.post('/api/i2c',json=other).status_code,409)
        self.c.post('/api/i2c/'+rid+'/cancel')
        response=self.c.post('/api/i2c',json=other);response.raise_for_status()
        rid=response.json()['id'];self.assertEqual(self.report('target','','idle')['kind'],'master')
        self.report('target',rid,'done',found=[]);self.assertEqual(self.row(rid)['state'],'completed')

    def test_external_validation_capability_and_range_reporting(self):
        base={**self.body,'mode':'external','target_session':None}
        for kw in ({'reply':'00'},{'target_session':self.nodes['target']['sid']},{'scan_start':7},{'scan_end':120},{'scan_start':0x50,'scan_end':0x40}):
            self.assertEqual(self.c.post('/api/i2c',json={**base,**kw}).status_code,422)
        self.poll('probe',capabilities=['debug_uart_v1','i2c_bench_v1'],debug={'session':self.nodes['probe']['sid'],'state':'active'})
        self.assertEqual(self.c.post('/api/i2c',json=base).status_code,409)
        self.report('probe','','idle')
        rid=self.external(scan_start=0x40,scan_end=0x47)
        self.report('probe','','idle');self.report('probe',rid,'done',found=[0x50])
        self.assertEqual(self.row(rid)['state'],'failed')

    def test_legacy_idempotency_parameters_still_resolve_to_original_record(self):
        rid=self.create()
        with server.db() as c:
            p=json.loads(c.execute('SELECT params FROM i2c_runs WHERE id=?',(rid,)).fetchone()[0])
            for k in ('mode','scan_start','scan_end','bus_key'):p.pop(k)
            c.execute('UPDATE i2c_runs SET params=? WHERE id=?',(json.dumps(p),rid))
        self.assertEqual(self.create(),rid)

    def test_migration_preserves_existing_rows_and_makes_responder_optional(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.executescript('''CREATE TABLE debug_sessions(id TEXT PRIMARY KEY);
          CREATE TABLE i2c_runs(id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
          master_session TEXT NOT NULL REFERENCES debug_sessions(id),target_session TEXT NOT NULL REFERENCES debug_sessions(id),params TEXT NOT NULL,
          state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,dispatched INTEGER NOT NULL DEFAULT 0,target_ready INTEGER NOT NULL DEFAULT 0,
          master_report TEXT NOT NULL DEFAULT '{}',target_report TEXT NOT NULL DEFAULT '{}',detail TEXT NOT NULL DEFAULT '',UNIQUE(owner,request_id));
          INSERT INTO i2c_runs VALUES('saved','owner','request','m','t','{}','completed',1,2,3,1,1,'{}','{}','saved detail');''')
        before=dict(c.execute('SELECT * FROM i2c_runs').fetchone())
        i2c_api.init(c);i2c_api.init(c)
        self.assertEqual(before,dict(c.execute('SELECT * FROM i2c_runs').fetchone()))
        info={r['name']:r for r in c.execute('PRAGMA table_info(i2c_runs)')}
        self.assertEqual(info['target_session']['notnull'],0);c.close()


if __name__=='__main__':unittest.main()

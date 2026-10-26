import contextlib
import io
import json
import secrets
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from jsonschema import Draft202012Validator

from edbg_cli.main import main
from edbg_sdk import Client, ClientError
from edbg_server import streams
from . import test_operations as base
from . import agent_runtime

server = base.server


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.fixture = base.OperationTests(methodName='runTest')
        self.fixture.setUp()
        server.debug_api.BENCH[self.fixture.did] = 'probe'
        self.opened = self.fixture.open()
        self.sid = self.opened['session_id']
        self.stream_id = self.opened['result']['stream_id']
        self.client = self.fixture.client

    def tearDown(self):
        self.fixture.tearDown()

    def inject(self, frames=(), dropped=0, errors=0, boot='test-boot', state='active', policy=None):
        body = {'boot_id':boot,'version':'3.5.0','runtime':agent_runtime(),'partition':'app0','uptime':1,'heap':100000,'min_heap':90000,
                'rssi':-40,'capabilities':['debug_uart_v1','i2c_master_v2'],
                'debug':{'session':self.sid,'state':state,'rx':list(frames),'dropped':dropped,'uart_errors':errors}}
        def send():
            result = self.client.post('/api/device/'+self.fixture.did+'/poll', json=body,
                                      headers={'Authorization':'Bearer '+self.fixture.token})
            result.raise_for_status()
            return result.json()['debug_control']
        if policy is None:
            return send()
        original = streams.ingest
        with patch.object(streams,'ingest',side_effect=lambda c,s,r,n:original(c,s,r,n,policy)):
            return send()

    def frame(self, seq, port=1, data='ab'):
        return {'seq':seq,'port':port,'us':seq*100,'data':data}

    def read(self, **params):
        response = self.client.get('/api/workspace/streams/'+self.stream_id+'/read',params=params)
        response.raise_for_status()
        return response.json()

    def test_two_consumers_get_same_bytes_and_reads_have_no_database_side_effects(self):
        control = self.inject([self.frame(1),self.frame(2,data='cdef')])
        self.assertEqual(control['rx_ack'],2)
        with server.db() as c:before='\n'.join(c.iterdump())
        first=self.read(); second=self.read()
        schema=json.loads((server.ROOT.parent/'contracts/compat/uart-stream-v0/read.schema.json').read_text(encoding='utf-8'))
        Draft202012Validator.check_schema(schema);Draft202012Validator(schema).validate(first)
        self.assertEqual(first,second)
        self.assertEqual(''.join(r['data_hex'] for r in first['items']),'abcdef')
        self.assertEqual(self.read(after=first['next_after'])['items'],[])
        with server.db() as c:self.assertEqual(before,'\n'.join(c.iterdump()))
        self.assertEqual(self.inject()['rx_ack'],2)

    def test_gap_counter_and_duplicate_reports_are_distinct_and_deduplicated(self):
        frames=[self.frame(3),self.frame(1),self.frame(3)]
        self.assertEqual(self.inject(frames,dropped=1,errors=2)['rx_ack'],3)
        page=self.read()
        self.assertEqual([r['kind'] for r in page['items']],['rx','source_gap','rx','source_counters'])
        self.assertEqual(page['items'][1]['missing_records'],1)
        self.assertTrue(page['items'][-1]['overlaps_sequence_gaps'])
        self.inject(frames,dropped=1,errors=2)
        self.assertEqual(self.read()['items'],page['items'])

    def test_transaction_failure_does_not_commit_data_or_source_ack(self):
        with patch.object(server.operations,'after_poll',side_effect=RuntimeError('Simulated commit interruption')):
            with self.assertRaises(RuntimeError):self.inject([self.frame(1)])
        self.assertEqual(self.read()['items'],[])
        self.assertEqual(self.read()['stream']['source_checkpoint'],0)
        self.assertEqual(self.inject([self.frame(1)])['rx_ack'],1)
        self.assertEqual(len(self.read()['items']),1)

    def test_retention_expires_cursor_and_recovery_is_explicit(self):
        cursor=self.read()['next_after']
        policy=streams.Retention(stream_records=2,stream_bytes=128,total_records=65536,total_bytes=8388608)
        self.inject([self.frame(n) for n in range(1,5)],policy=policy)
        response=self.client.get('/api/workspace/streams/'+self.stream_id+'/read',params={'after':cursor})
        self.assertEqual(response.status_code,410)
        context=response.json()['detail']['context']
        self.assertEqual(context['trimmed_through'],2)
        recovered=self.read(after=context['earliest_after'])
        self.assertEqual([r['source_seq'] for r in recovered['items']],[3,4])
        self.assertTrue(recovered['history_truncated'])
        with server.db() as c:
            c.execute('DELETE FROM debug_rx WHERE session_id=?',(self.sid,))
        self.assertEqual(self.inject()['rx_ack'],4)

    def test_global_budget_prunes_prefixes_and_preserves_exact_accounting(self):
        policy=streams.Retention(stream_records=100,stream_bytes=12800,total_records=2,total_bytes=128)
        self.inject([self.frame(n) for n in range(1,5)],policy=policy)
        with server.db() as c:
            budget=c.execute('SELECT records,bytes FROM workspace_stream_budget').fetchone()
            actual=c.execute('SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM workspace_stream_records').fetchone()
            sums=c.execute('SELECT SUM(retained_records),SUM(retained_bytes) FROM workspace_streams').fetchone()
            self.assertEqual(tuple(budget),tuple(actual));self.assertEqual(tuple(sums),tuple(actual))
            self.assertLessEqual(budget['records'],2);self.assertLessEqual(budget['bytes'],128)
        self.assertEqual([r['source_seq'] for r in self.read()['items']],[3,4])

    def test_port_filter_advances_over_other_ports_and_byte_limit_keeps_progress(self):
        self.inject([self.frame(1,port=2),self.frame(2,port=1,data='aa'*128),self.frame(3,port=1,data='bb'*128)])
        first=self.read(port=1,limit=1)
        self.assertEqual(first['items'],[]);self.assertEqual(first['scanned_records'],1)
        second=self.read(after=first['next_after'],port=1,limit_bytes=128)
        self.assertEqual(len(second['items']),1);self.assertTrue(second['has_more'])
        third=self.read(after=second['next_after'],port=1,limit_bytes=128)
        self.assertEqual(third['items'][0]['source_seq'],3)
        self.assertFalse(third['has_more'])

    def test_bad_cross_stream_and_future_cursors_are_rejected(self):
        for cursor in ('nonsense','c1.!!!',streams.token('another-stream',0),streams.token(self.stream_id,100)):
            with self.subTest(cursor=cursor):
                response=self.client.get('/api/workspace/streams/'+self.stream_id+'/read',params={'after':cursor})
                self.assertEqual(response.status_code,422)
        self.assertEqual(self.client.get('/api/workspace/streams/'+self.stream_id+'/read?limit_bytes=1').status_code,422)

    def test_close_marks_end_but_never_claims_complete_tail(self):
        self.inject([self.frame(1)])
        self.fixture.close(self.sid)
        page=self.read()
        self.assertTrue(page['eof']);self.assertTrue(page['stream']['source_ended'])
        self.assertFalse(page['stream']['tail_complete'])
        self.assertEqual(page['items'][-1]['kind'],'source_ended')

    def test_final_expired_snapshot_frames_are_saved_before_source_end(self):
        self.inject([self.frame(1)])
        self.inject([self.frame(2)],state='expired')
        page=self.read()
        self.assertEqual([r['source_seq'] for r in page['items'] if r['kind']=='rx'],[1,2])
        self.assertTrue(page['eof']);self.assertFalse(page['stream']['tail_complete'])

    def test_never_published_cancel_marks_stream_end_without_waiting_for_a_poll(self):
        fixture=base.OperationTests(methodName='runTest');fixture.setUp()
        try:
            accepted=fixture.submit()
            response=fixture.client.post('/api/workspace/operations/'+accepted['id']+'/cancel')
            self.assertEqual(response.json()['outcome'],'cancelled')
            page=fixture.client.get('/api/workspace/streams/'+streams.identity(accepted['session_id'])+'/read').json()
            self.assertTrue(page['eof']);self.assertTrue(page['stream']['source_ended'])
            self.assertEqual(page['items'][-1]['kind'],'source_ended')
            self.assertEqual(page['items'][-1]['end_evidence'],'server_never_published')
        finally:fixture.tearDown()

    def test_new_boot_ends_old_stream_and_does_not_mix_new_frames(self):
        self.inject([self.frame(1)])
        self.inject([self.frame(2,data='ff')],boot='next-boot')
        page=self.read()
        self.assertTrue(page['eof']);self.assertEqual(page['stream']['boot_id'],'test-boot')
        self.assertEqual([r['source_seq'] for r in page['items'] if r['kind']=='rx'],[1])
        self.assertEqual(page['items'][-1]['reason'],'lost')

    def test_legacy_backfill_is_marked_incomplete_and_import_is_idempotent(self):
        sid=secrets.token_hex(12);now=time.time()
        binding=self.opened['binding']
        with server.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO debug_sessions(id,device_id,role,boot_id,owner,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                (sid,self.fixture.did,'probe','test-boot','workspace:local-admin','closed',now,now,now))
            c.execute('INSERT INTO workspace_uart_sessions(session_id,principal,connection_id,connection_revision,binding) VALUES(?,?,?,?,?)',
                (sid,'local-admin',self.fixture.connection['id'],1,json.dumps(binding)))
            c.execute('INSERT INTO debug_rx(session_id,seq,port,us,data,created) VALUES(?,?,?,?,?,?)',(sid,5,1,500,'beef',now))
            c.execute("DELETE FROM workspace_imports WHERE name='uart-rx-streams-v1'")
            streams.import_existing(c,now)
            before=c.execute('SELECT COUNT(*) FROM workspace_stream_records').fetchone()[0]
            streams.import_existing(c,now)
            self.assertEqual(before,c.execute('SELECT COUNT(*) FROM workspace_stream_records').fetchone()[0])
        page=self.client.get('/api/workspace/streams/'+streams.identity(sid)+'/read').json()
        self.assertEqual(page['stream']['origin'],'legacy_backfill')
        self.assertFalse(page['stream']['origin_at_session_start'])
        self.assertTrue(any(r['kind']=='history_unavailable' for r in page['items']))
        self.assertEqual([r['data_hex'] for r in page['items'] if r['kind']=='rx'],['beef'])

    def test_sdk_and_cli_read_follow_and_expiry_return_machine_checkpoints(self):
        factory=lambda url,**kw:Client('http://testserver',http_client=self.client)
        self.inject([self.frame(1)])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['stream','read',self.stream_id,'--json'],client_factory=factory)
        page=json.loads(output.getvalue())['data'];self.assertEqual(code,0)
        cursor=page['next_after']
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['stream','follow',self.stream_id,'--after',cursor,'--duration','0.02','--interval','0.005'],client_factory=factory)
        lines=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(code,0);self.assertEqual(lines[-1]['kind'],'checkpoint')
        self.assertEqual(lines[-1]['resume_after'],cursor)
        self.inject([self.frame(n) for n in range(2,6)],policy=streams.Retention(stream_records=1))
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['stream','read',self.stream_id,'--after',cursor,'--json'],client_factory=factory)
        result=json.loads(output.getvalue())
        self.assertEqual(code,10);self.assertEqual(result['error']['code'],'CURSOR_EXPIRED')
        self.assertIn('earliest_after',result['error']['context'])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['stream','follow',self.stream_id,'--after',cursor,'--duration','0.02'],client_factory=factory)
        result=json.loads(output.getvalue())
        self.assertEqual(code,10);self.assertEqual(result['kind'],'error')
        self.assertEqual(result['resume_after'],cursor)
        self.assertIn('earliest_after',result['error']['context'])


if __name__=='__main__':unittest.main()

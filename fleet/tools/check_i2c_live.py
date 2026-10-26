"""Exercise only the fixed B9/10 -> A9/10, 3.3 V, internally pulled-up bench."""
import json
import argparse
import secrets
import time
from pathlib import Path
from client import FleetClient

ROOT=Path(__file__).resolve().parents[1]


def wait(fn,seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=fn()
        if value:return value
        time.sleep(1)
    raise TimeoutError('No device confirmation')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--boundaries',action='store_true');args=parser.parse_args()
    c=FleetClient();sessions={};out={'time':time.time(),'checks':[]}
    path=ROOT/('artifacts/i2c-boundary-check.json' if args.boundaries else 'artifacts/i2c-live-check.json')
    def save():path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for did,role in c.get('/api/debug')['bench'].items():
            sid=c.post('/api/debug/sessions',{'device_id':did})['id'];sessions[role]=sid
            wait(lambda:any(s['id']==sid and s['state']=='active' for s in c.get('/api/debug')['sessions']))
        vector=bytes(range(64)).hex()
        cases=[
            ('scan','',0,'',0x42),('read','',16,vector[:32],0x42),
            ('write','a50001ff',0,'',0x42),('write_read','01020304',4,'deadbeef',0x42),
            ('write_read',vector,64,vector,0x42),('read','',1,'00',0x43)]
        if args.boundaries:cases=[('write_read',vector[:n*2],n,vector[:n*2],0x42) for n in (1,27,28,29,32,63,64)]
        for op,write,read,reply,address in cases:
            body={'master_session':sessions['probe'],'target_session':sessions['target'],'operation':op,'data':write,'read_len':read,'reply':reply,'address':address,'request_id':secrets.token_hex(12)}
            rid=c.post('/api/i2c',body)['id']
            assert c.post('/api/i2c',body)['id']==rid
            row=wait(lambda:(r if (r:=c.get('/api/i2c/'+rid))['state'] not in ('arming','running','verifying') else None))
            out['checks'].append(row);save()
            print(op,hex(address),row['state'],row['detail'],flush=True)
            if address==0x43:
                assert row['state']=='failed' and 'NACK' in row['detail'],row
            else:
                assert row['state']=='completed',row
                if op=='scan':assert row['master_report']['found']==[0x42],row
                if write:assert row['target_report']['data']==write,row
                if read:assert row['master_report']['data']==reply,row
            assert c.post('/api/i2c',body)['id']==rid
        if args.boundaries:
            # Confirm the I2C driver has not broken the existing shared sessions.
            out['uart']=[]
            for source,dest in [('target','probe'),('probe','target')]:
                payload=b'I2C-regression-'+secrets.token_bytes(4)+b'\x00\xff'
                tx=c.post('/api/debug/sessions/'+sessions[source]+'/send',{'data':payload.hex(),'request_id':secrets.token_hex(12)})
                def received():
                    rows=c.get('/api/debug/sessions/'+sessions[dest]+'/rx')
                    stream=bytes.fromhex(''.join(r['data'] for r in rows if r['port']==1))
                    return stream if payload in stream else None
                stream=wait(received);out['uart'].append({'direction':source+'->'+dest,'bytes':len(payload),'matched':True,'tx':tx})
            wid=c.post('/api/waves',{'receiver_session':sessions['probe'],'source_session':sessions['target'],'mode':'paired','high_us':300,'low_us':700,'count':16,'request_id':secrets.token_hex(12)})['id']
            wave=wait(lambda:(r if (r:=c.get('/api/waves/'+wid))['state'] not in ('arming','capturing') else None))
            assert wave['state']=='completed' and wave['stats']['high_pulses']==16,wave
            out['wave']=wave
            print('UART bidirectional and 16-pulse waveform regression passed.',flush=True)
        out['all_passed']=True
    finally:
        for sid in sessions.values():c.post('/api/debug/sessions/'+sid+'/close',{})
        wait(lambda:all(s['state'] in ('closed','lost') for s in c.get('/api/debug')['sessions'] if s['id'] in sessions.values()))
        out['sessions_closed']=True;save()


if __name__=='__main__':main()

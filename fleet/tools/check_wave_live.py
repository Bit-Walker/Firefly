"""Finite pulse verification on the wired A6->B6, B7->A7 bench."""
import json
import argparse
import secrets
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--bounds',action='store_true');args=parser.parse_args()
    output=ROOT/('artifacts/wave-boundaries-live-check.json' if args.bounds else 'artifacts/wave-live-check.json')
    evidence={'time':time.time(),'checks':[]}
    with httpx.Client(base_url='http://127.0.0.1:8088',headers={'Origin':'http://127.0.0.1:8088'},trust_env=False,timeout=15) as c:
        def get(path):
            r=c.get(path);r.raise_for_status();return r.json()
        def post(path,body):
            r=c.post(path,json=body);r.raise_for_status();return r.json()
        def wait(fn,seconds=45):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                value=fn()
                if value:return value
                time.sleep(1)
            raise RuntimeError('Device result timeout')
        post('/api/login',json.loads((ROOT/'.state/admin.local.json').read_text(encoding='utf-8-sig')))
        sessions={}
        try:
            for did,role in get('/api/debug')['bench'].items():
                sid=post('/api/debug/sessions',{'device_id':did})['id'];sessions[role]=sid
                wait(lambda:any(s['id']==sid and s['state']=='active' for s in get('/api/debug')['sessions']))
            cases=[('ba',1000,1000,1),('ab',50,50,64),('ba',10000,10000,50)] if args.bounds else [('ab',500,500,16),('ba',250,750,24),('ab',100,100,64)]
            for direction,hi,lo,count in cases:
                body={'receiver_session':sessions['probe' if direction=='ab' else 'target'],'source_session':sessions['target' if direction=='ab' else 'probe'],'mode':'paired','high_us':hi,'low_us':lo,'count':count,'request_id':secrets.token_hex(12)}
                wid=post('/api/waves',body)['id']
                assert post('/api/waves',body)['id']==wid
                def finished():
                    row=get('/api/waves/'+wid)
                    return row if row['state'] not in ('arming','capturing') else None
                row=wait(finished)
                evidence['checks'].append(row)
                output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
                assert row['state']=='completed',row
                stats=row['stats']
                assert stats['high_pulses']==count,stats
                assert abs(stats['high_us_mean']-hi)<=5,stats
                if count>1:
                    assert abs(stats['low_us_mean']-lo)<=5,stats
                    assert abs(stats['frequency_hz']-1000000/(hi+lo))<=1000000/(hi+lo)*0.03,stats
                else:
                    assert stats['low_us_mean'] is None and stats['frequency_hz'] is None,stats
                assert not row['capture']['truncated']
                print('Wave verified:',direction,'pulses=',count,'high=',stats['high_us_mean'],'low=',stats['low_us_mean'],'Hz=',stats['frequency_hz'],flush=True)
            if args.bounds:return
            body={'receiver_session':sessions['probe'],'mode':'capture','request_id':secrets.token_hex(12)}
            wid=post('/api/waves',body)['id']
            time.sleep(4)
            post('/api/waves/'+wid+'/cancel',{})
            row=get('/api/waves/'+wid)
            assert row['state']=='cancelled' and not row['source_session']
            evidence['cancel']=row
            # Wait through a whole no-trigger capture to verify the device's own
            # bounded receive timeout without ever issuing an output command.
            wid=post('/api/waves',{**body,'request_id':secrets.token_hex(12)})['id']
            row=wait(lambda:(r if (r:=get('/api/waves/'+wid))['state'] not in ('arming','capturing') else None))
            assert row['state']=='failed' and 'No pulse trigger' in row['detail'],row
            evidence['no_trigger']=row
            print('Capture-only cancel and no-trigger timeout verified.',flush=True)
        finally:
            for sid in sessions.values():post(f'/api/debug/sessions/{sid}/close',{})
            wait(lambda:all(s['state'] in ('closed','lost') for s in get('/api/debug')['sessions'] if s['id'] in sessions.values()))
            evidence['final_sessions']=[s for s in get('/api/debug')['sessions'] if s['id'] in sessions.values()]
            output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

"""Install/verify the UART milestone on the explicitly wired local A/B bench."""
import argparse
import json
import secrets
import time
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
A='s3-fc012cd58460'
B='s3-fc012cca2cf8'


def wait(fn, seconds=45):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        result=fn()
        if result:return result
        time.sleep(1)
    raise RuntimeError('Timed out waiting for device confirmation')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--install',action='store_true')
    parser.add_argument('--test',action='store_true')
    parser.add_argument('--version',default='3.3.1')
    args=parser.parse_args()
    evidence={'checks':[],'time':time.time()}
    with httpx.Client(base_url='http://127.0.0.1:8088',headers={'Origin':'http://127.0.0.1:8088'},timeout=15,trust_env=False) as client:
        def get(path):
            response=client.get(path);response.raise_for_status();return response.json()
        def post(path,data):
            response=client.post(path,json=data);response.raise_for_status();return response.json()
        post('/api/login',json.loads((ROOT/'.state/admin.local.json').read_text(encoding='utf-8-sig')))
        if args.install:
            response=client.post('/api/firmware',content=(ROOT/f'artifacts/{args.version}/fleet_agent.bin').read_bytes(),headers={'Content-Type':'application/octet-stream'})
            response.raise_for_status();firmware=response.json();evidence['firmware']=firmware
            for device_id in (B,A):
                device=next(d for d in get('/api/devices') if d['id']==device_id)
                if not device['online']:raise RuntimeError('Bench device offline')
                if device['version']==args.version and 'debug_uart_v1' in device['telemetry'].get('capabilities',[]):continue
                jid=post('/api/jobs',{'device_ids':[device_id],'kind':'ota','firmware_id':firmware['id']})['job_ids'][0]
                def completed():
                    job=next(j for j in get('/api/jobs') if j['id']==jid)
                    if job['state']=='failed':raise RuntimeError(job['detail'])
                    return job if job['state']=='succeeded' else None
                job=wait(completed,120)
                evidence['checks'].append({'ota':device_id,'job':job})
                print('OTA confirmed:',device_id,flush=True)
            evidence['devices']=get('/api/devices')
            (ROOT/'artifacts/debug-install-check.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.test:
            sessions=[]
            def row(sid):return next(s for s in get('/api/debug')['sessions'] if s['id']==sid)
            def close(sid):
                post(f'/api/debug/sessions/{sid}/close',{})
                wait(lambda:row(sid)['state']=='closed')
            def received(sid,port=1):
                return bytes.fromhex(''.join(r['data'] for r in get(f'/api/debug/sessions/{sid}/rx') if r['port']==port))
            try:
                for device_id in (A,B):
                    sid=post('/api/debug/sessions',{'device_id':device_id})['id'];sessions.append(sid)
                    wait(lambda:row(sid)['state']=='active')
                sa,sb=sessions
                for src,dst,payload in [(sa,sb,('A-to-B-'+secrets.token_hex(6)+'\r\n').encode()),(sb,sa,b'\x00\x55\xaa\xff'+('B-to-A-'+secrets.token_hex(6)).encode())]:
                    body={'data':payload.hex(),'request_id':secrets.token_hex(12)}
                    tx=post(f'/api/debug/sessions/{src}/send',body)['id']
                    assert post(f'/api/debug/sessions/{src}/send',body)['id']==tx
                    wait(lambda:any(t['id']==tx and t['state']=='sent' for t in row(src)['tx']))
                    wait(lambda:payload in received(dst))
                    time.sleep(3)
                    assert received(dst).count(payload)==1,'Duplicate physical UART transmission'
                    evidence['checks'].append({'uart_bidirectional':True,'source':src,'target':dst,'bytes':len(payload),'hex':payload.hex(),'tx_id':tx,'physical_occurrences':1})
                    print('UART verified:',len(payload),'bytes; duplicate request did not duplicate RX',flush=True)
                close(sa)
                # A can restart while the independent B probe continues receiving.
                old_boot=next(d['boot_id'] for d in get('/api/devices') if d['id']==A)
                jid=post('/api/jobs',{'device_ids':[A],'kind':'restart'})['job_ids'][0]
                wait(lambda:any(j['id']==jid and j['state']=='succeeded' for j in get('/api/jobs')),60)
                wait(lambda:b'ESP-ROM' in received(sb,2) or b'boot:' in received(sb,2),30)
                wait(lambda:b'fleet: Device=' in received(sb,2),30)
                new_boot=next(d['boot_id'] for d in get('/api/devices') if d['id']==A)
                assert old_boot!=new_boot
                evidence['checks'].append({'independent_boot_monitor':True,'restart_job':jid,'old_boot':old_boot,'new_boot':new_boot,'boot_rx_bytes':len(received(sb,2))})
                print('Independent probe captured target reboot log.',flush=True)
                evidence['sessions']=[row(s) for s in sessions]
                evidence['rx']={s:get(f'/api/debug/sessions/{s}/rx') for s in sessions}
            finally:
                for sid in sessions:
                    if row(sid)['state'] in ('opening','active','expired','closing'):close(sid)
                evidence['final_sessions']=[row(s) for s in sessions]
                (ROOT/'artifacts/debug-uart-live-check.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()

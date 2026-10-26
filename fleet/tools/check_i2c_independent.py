"""Verify standalone masters on the wired bus while the peer responder is idle.

No external sensor is assumed. Successful empty scans and explicit NACK results
verify independence and line checks; positive data transfers use the paired suite.
"""
import json
import secrets
import time
from pathlib import Path
from client import FleetClient

ROOT=Path(__file__).resolve().parents[1]


def wait(fn,seconds=40):
    until=time.monotonic()+seconds
    while time.monotonic()<until:
        value=fn()
        if value:return value
        time.sleep(1)
    raise TimeoutError('No confirmed device result')


def main():
    c=FleetClient();out={'time':time.time(),'external_peripheral_connected':False,'checks':[]};opened=[]
    path=ROOT/'artifacts/independent-i2c-live-check.json'
    ids=list(c.get('/api/debug')['bench'])
    def save():path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    def open_session(did):
        sid=c.post('/api/debug/sessions',{'device_id':did})['id'];opened.append(sid)
        wait(lambda:any(s['id']==sid and s['state']=='active' for s in c.get('/api/debug')['sessions']))
        return sid
    def close(sid):
        c.post('/api/debug/sessions/'+sid+'/close',{})
        wait(lambda:any(s['id']==sid and s['state']=='closed' for s in c.get('/api/debug')['sessions']))
        opened.remove(sid)
    def latest(did):
        rows=c.get('/api/debug/sessions?device_id='+did+'&limit=1')['sessions']
        return rows[0]['id'] if rows else None
    try:
        assert not any(s['state'] in ('opening','active','closing','expired') for s in c.get('/api/debug')['sessions'])
        for index,did in enumerate(ids):
            peer=ids[1-index];before=latest(peer);sid=open_session(did)
            for operation in ('scan','read'):
                body={'mode':'external','master_session':sid,'operation':operation,'scan_start':0x40,'scan_end':0x47,'address':0x42,'read_len':1 if operation=='read' else 0,'request_id':secrets.token_hex(12)}
                rid=c.post('/api/i2c',body)['id'];assert c.post('/api/i2c',body)['id']==rid
                row=wait(lambda:(r if (r:=c.get('/api/i2c/'+rid))['state'] not in ('arming','running','verifying') else None))
                out['checks'].append(row);save()
                assert row['target'] is None and row['target_report']=={}
                assert row['master_report']['sda_idle']==row['master_report']['scl_idle']==1,row
                if operation=='scan':assert row['state']=='completed' and row['master_report']['found']==[],row
                else:assert row['state']=='failed' and 'NACK' in row['detail'],row
                assert latest(peer)==before,'Standalone operation created a peer session'
                print(did,operation,row['state'],'SDA/SCL high; no responder session',flush=True)
            close(sid)
        first,second=(open_session(did) for did in ids)
        rid=c.post('/api/i2c',{'mode':'external','master_session':first,'scan_start':0x40,'scan_end':0x47,'request_id':secrets.token_hex(12)})['id']
        response=c.http.post('/api/i2c',json={'mode':'external','master_session':second,'request_id':secrets.token_hex(12)})
        assert response.status_code==409,response.text
        out['shared_bus_conflict']={'status':response.status_code,'detail':response.json()['detail']}
        c.post('/api/i2c/'+rid+'/cancel',{})
        out['all_passed']=True
        print('Shared physical bus rejected a second master transaction.',flush=True)
    finally:
        for sid in list(opened):close(sid)
        out['sessions_closed']=True;save()


if __name__=='__main__':main()

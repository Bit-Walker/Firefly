"""Node updates confirmed by a new healthy boot and its measured image bytes."""
import hashlib
import json
import secrets

from fastapi import HTTPException

from edbg_server import artifacts, operations as ops, planning, runtime
from edbg_server.workspace import reject


def link(c,identity):
    return c.execute('SELECT * FROM workspace_update_operations WHERE operation_id=?',(identity,)).fetchone()


def for_job(c,identity,node_id=None):
    where,args='u.job_id=?',[identity]
    if node_id is not None:where+=' AND u.node_id=?';args.append(node_id)
    return c.execute('SELECT o.* FROM workspace_operations o JOIN workspace_update_operations u ON u.operation_id=o.id WHERE '+where,args).fetchone()


def conclude(c,row,now,outcome,code,released,level='server_ledger'):
    binding=link(c,row['id']);pinned=json.loads(row['binding']);report=json.loads(binding['report'])
    measured=json.loads(binding['report_runtime'])
    verified=outcome=='succeeded' and code=='UPDATE_IMAGE_IDENTITY_CONFIRMED'
    result={'code':code,'job_id':binding['job_id'],'image':pinned['image'],
            'confirmation':'image_identity','runtime_image_identity_verified':verified,
            'observed_runtime':measured,'verification':'node_flash_sha256' if verified else 'none',
            'source_boot_id':pinned['node_boot_id'],'observed_boot_id':binding['report_boot'],
            'observed_version':binding['report_version'],'observed_partition':binding['report_partition'],
            'node_report':report,'effect':'possible' if row['published'] else 'none'}
    if outcome=='succeeded':result['effect']='confirmed'
    evidence=json.loads(row['evidence'])
    if not evidence or evidence[-1].get('code')!=code:
        evidence.append({'code':code,'level':level,'observed_at':now,'boot_id':binding['report_boot'] or pinned['node_boot_id']})
    if released:c.execute('UPDATE workspace_update_operations SET held=0 WHERE operation_id=?',(row['id'],))
    ops.change(c,row['id'],now,'update.reconciled' if row['state']=='finished' else 'update.finished',
               state='finished',outcome=outcome,resource_status='released' if released else 'unconfirmed',
               result=ops.dump(result),evidence=ops.dump(evidence))


def refresh_one(c,row,now):
    binding=link(c,row['id'])
    if binding is None:raise RuntimeError('Node update is missing its job binding')
    if not binding['held']:return
    job=c.execute('SELECT * FROM jobs WHERE id=?',(binding['job_id'],)).fetchone()
    pinned=json.loads(row['binding'])
    node=c.execute('SELECT * FROM devices WHERE id=?',(row['node_id'],)).fetchone()
    if not row['published']:
        image=artifacts.lookup(c,pinned['image']['artifact_id'])
        code=None;outcome='failed'
        if node['revoked']:code='NODE_REVOKED_BEFORE_DISPATCH'
        elif node['boot_id']!=pinned['node_boot_id']:code='BOOT_CHANGED_BEFORE_DISPATCH'
        elif not runtime.supports(node,'node.update/1') or not runtime.matches(node,pinned):code='AGENT_CHANGED_BEFORE_DISPATCH'
        elif image is None or image['withdrawn']:code='ARTIFACT_WITHDRAWN'
        elif job['state']=='cancelled':code='UPDATE_CANCELLED_BEFORE_PUBLICATION';outcome='cancelled'
        elif now>=row['dispatch_before']:code='DISPATCH_DEADLINE_EXCEEDED';outcome='expired'
        if code:
            c.execute("UPDATE jobs SET state='cancelled',updated=?,detail=? WHERE id=? AND state='queued'",(now,code,binding['job_id']))
            conclude(c,row,now,outcome,code,True)
    elif row['execution_deadline'] is not None and now>=row['execution_deadline']:
        conclude(c,row,now,'unknown','UPDATE_CONFIRMATION_MISSING',False)


def submit(c,document,normalized,bench,now):
    assessment=planning.assess(c,document,bench,ops.OWNER,now)
    if assessment['status']!='supported':
        raise HTTPException(409,{'code':'OPERATION_NOT_READY','message':'Node maintenance or image is not ready.','assessment':assessment})
    identity,jid=secrets.token_hex(12),secrets.token_hex(12)
    binding=assessment['binding'];did=binding['node_id']
    c.execute("INSERT INTO jobs(id,device_id,kind,firmware_id,state,created,updated) VALUES(?,?,'ota',?,'queued',?,?)",
              (jid,did,binding['image']['artifact_id'],now,now))
    c.execute('''INSERT INTO workspace_operations
        (id,principal,idempotency_key,request,request_digest,action,node_id,binding,resources,
         state,resource_status,created,updated,dispatch_before,execution_ms)
        VALUES(?,?,?,?,?,'node.update',?,?,?,'accepted','held',?,?,?,?)''',
        (identity,ops.PRINCIPAL,document['idempotency_key'],normalized,hashlib.sha256(normalized.encode()).hexdigest(),did,
         ops.dump(binding),ops.dump(assessment['resources']),now,now,now+document['timing']['dispatch_within_ms']/1000,
         document['timing']['execution_timeout_ms']))
    c.execute('INSERT INTO workspace_update_operations(operation_id,job_id,node_id) VALUES(?,?,?)',(identity,jid,did))
    ops.change(c,identity,now,'operation.accepted')
    return ops.view(ops.find(c,identity))


def publish(c,row,now):
    refresh_one(c,row,now);row=ops.find(c,row['id'])
    if row['published'] or row['state']=='finished':return None
    pinned=json.loads(row['binding']);image=artifacts.lookup(c,pinned['image']['artifact_id'])
    if not artifacts.check_file(image):
        binding=link(c,row['id'])
        c.execute("UPDATE jobs SET state='cancelled',updated=?,detail='Artifact integrity check failed' WHERE id=?",(now,binding['job_id']))
        conclude(c,row,now,'failed','ARTIFACT_INTEGRITY_FAILED',True)
        return None
    binding=link(c,row['id'])
    c.execute("UPDATE jobs SET state='dispatched',source_boot=?,updated=? WHERE id=? AND state='queued'",(pinned['node_boot_id'],now,binding['job_id']))
    ops.change(c,row['id'],now,'update.command_published',state='dispatched',published=1,execution_deadline=now+row['execution_ms']/1000,
               result=ops.dump({'stage':'dispatched','job_id':binding['job_id'],'runtime_image_identity_verified':False}))
    return {'id':binding['job_id'],'kind':'ota','firmware_id':image['id'],'target_version':image['version'],'sha256':image['sha256'],'size':image['size']}


def process_report(c,row,body,now):
    """Return an ack only after terminal evidence has been persisted and validated."""
    if not row['published']:return None
    binding=link(c,row['id']);report=body.report
    prior=json.loads(binding['report'])
    if prior.get('state') in ('succeeded','failed'):return report.id
    pinned=json.loads(row['binding'])
    if report.state=='succeeded' and (body.boot_id==pinned['node_boot_id'] or body.version!=pinned['image']['version']):
        raise HTTPException(409,'OTA success requires new boot and target version')
    measured=body.runtime
    outcome=report.state;code='UPDATE_NODE_FAILED'
    if report.state=='succeeded':
        if not measured or measured.health!='valid' or measured.image is None or measured.family!=pinned['agent']['family']:
            raise HTTPException(409,'OTA success requires a healthy runtime image report')
        matches=(measured.image.sha256==pinned['image']['sha256'] and measured.image.size==pinned['image']['size']
                 and measured.image.project=='fleet_agent' and 'runtime.image/1' in measured.features)
        outcome,code=('succeeded','UPDATE_IMAGE_IDENTITY_CONFIRMED') if matches else ('failed','UPDATE_IMAGE_MISMATCH')
    if report.state in ('running','rebooting') and body.boot_id!=pinned['node_boot_id']:
        return None
    encoded=report.model_dump_json()
    changed=encoded!=binding['report'] or binding['report_boot']!=body.boot_id
    c.execute('''UPDATE workspace_update_operations SET report=?,report_boot=?,report_version=?,report_partition=?,report_runtime=? WHERE operation_id=?''',
              (encoded,body.boot_id,body.version,body.partition,measured.model_dump_json() if measured else '{}',row['id']))
    c.execute('UPDATE jobs SET state=?,progress=?,detail=?,updated=? WHERE id=?',
              (outcome,report.progress,report.detail,now,binding['job_id']))
    if report.state in ('succeeded','failed'):
        conclude(c,row,now,outcome,code,True,'node_report')
        return report.id
    if changed:
        result={'stage':report.state,'job_id':binding['job_id'],'progress':report.progress,'node_report':report.model_dump(),
                'runtime_image_identity_verified':False}
        # Do not turn a timed-out client result back into an unfinished operation.
        if row['state']=='finished':
            result={**json.loads(row['result']),**result}
        ops.change(c,row['id'],now,'update.progress',result=ops.dump(result))
    return None


def cancel(c,row,now):
    if row['state']=='finished':return ops.view(row)
    if row['published']:
        reject('CANCELLATION_UNSUPPORTED','Published OTA cannot be reliably stopped; continue checking its original operation.',422)
    binding=link(c,row['id'])
    c.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=? AND state='queued'",(now,binding['job_id']))
    ops.change(c,row['id'],now,'cancel.requested',cancel_requested=1)
    conclude(c,ops.find(c,row['id']),now,'cancelled','UPDATE_CANCELLED_BEFORE_PUBLICATION',True)
    return ops.view(ops.find(c,row['id']))

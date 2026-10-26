"""Finite I2C master intents using an owned, temporary legacy debug context."""
import hashlib
import json
import secrets

from fastapi import HTTPException

from edbg_server import operations as ops, planning, runtime
from edbg_server.api import i2c as legacy
from edbg_server.workspace import reject

CLEANUP_SECONDS = 10


def link(c, identity):
    return c.execute('SELECT * FROM workspace_i2c_operations WHERE operation_id=?',(identity,)).fetchone()


def shape(document):
    messages=document['params']['messages']
    written=next((m['write_hex'].lower() for m in messages if 'write_hex' in m),'')
    count=next((m['read_length'] for m in messages if 'read_length' in m),0)
    return ('write_read' if written and count else 'write' if written else 'read'),written,count


def result(row, binding, released=False):
    request=json.loads(row['request']);operation,written,count=shape(request)
    report=json.loads(binding['bus_report'])
    complete=binding['bus_outcome']=='succeeded'
    not_started=binding['bus_code']=='I2C_BUS_NOT_IDLE' and (report.get('sda_idle')==0 or report.get('scl_idle')==0)
    return {'code':binding['bus_code'],'mode':'external','operation':operation,'address':request['params']['address'],
        'speed_hz':request['params']['speed_hz'],'bus_outcome':binding['bus_outcome'],
        'requested_write_bytes':len(written)//2,'requested_read_bytes':count,
        'confirmed_write_bytes':len(written)//2 if complete else 0 if not_started or not written else None,
        'confirmed_read_bytes':count if complete else 0 if not_started or not count else None,
        'read_data_hex':report.get('data','').lower(),'read_complete':complete,
        'observed_read_bytes':len(report.get('data',''))//2 if report else None,
        'bus_effect':'confirmed' if complete else 'none' if not_started else 'possible' if row['published'] else 'none',
        'effect':'confirmed' if complete else 'possible' if row['published'] and not not_started else 'preparation_only' if binding['context_published'] else 'none',
        'verification':'master_report' if report else 'none','master_report':report,
        'context_owned':True,'context_uart_enabled':True,'context_published':bool(binding['context_published']),
        'cleanup_confirmed':released,'session_id':row['session_id'],'partial_byte_progress':'not_started' if not_started else 'not_available'}


def begin_cleanup(c,row,now,outcome,code):
    binding=link(c,row['id'])
    c.execute('UPDATE workspace_i2c_operations SET bus_outcome=?,bus_code=?,cleanup_deadline=COALESCE(cleanup_deadline,?) WHERE operation_id=?',
              (outcome,code,now+CLEANUP_SECONDS,row['id']))
    state='closed' if not binding['context_published'] and not row['published'] else 'closing'
    c.execute("UPDATE debug_sessions SET state=?,updated=? WHERE id=? AND state NOT IN ('closed','lost')",(state,now,row['session_id']))
    if binding['run_id']:
        c.execute("UPDATE i2c_runs SET state=?,updated=? WHERE id=? AND state IN ('arming','running','verifying')",
                  ('unknown' if row['published'] else 'cancelled',now,binding['run_id']))
    binding=link(c,row['id'])
    evidence=json.loads(row['evidence'])
    if not evidence or evidence[-1].get('code')!=code:
        evidence.append({'code':code,'level':'master_report' if json.loads(binding['bus_report']) else 'server_ledger',
                         'observed_at':now,'boot_id':json.loads(row['binding'])['node_boot_id']})
    ops.change(c,row['id'],now,'i2c.releasing',state='releasing',outcome=None,resource_status='release_pending',
               result=ops.dump(result(row,binding)),evidence=ops.dump(evidence))


def release_or_wait(c,row,now):
    binding=link(c,row['id'])
    session=c.execute('SELECT state FROM debug_sessions WHERE id=?',(row['session_id'],)).fetchone()
    released=session['state'] in ('closed','lost')
    if released:
        c.execute('UPDATE workspace_i2c_operations SET held=0 WHERE operation_id=?',(row['id'],))
        evidence=json.loads(row['evidence'])
        if not evidence or evidence[-1].get('code')!='CONTEXT_RELEASE_CONFIRMED':
            evidence.append({'code':'CONTEXT_RELEASE_CONFIRMED','level':'server_ledger' if not binding['context_published'] else 'node_report',
                             'observed_at':now,'boot_id':json.loads(row['binding'])['node_boot_id']})
        ops.change(c,row['id'],now,'i2c.finished',state='finished',outcome=binding['bus_outcome'],resource_status='released',
                   result=ops.dump(result(row,binding,True)),evidence=ops.dump(evidence))
    elif binding['cleanup_deadline'] is not None and now>=binding['cleanup_deadline']:
        ops.change(c,row['id'],now,'i2c.release_unconfirmed',state='finished',outcome='unknown',resource_status='unconfirmed',
                   result=ops.dump(result(row,binding)))


def refresh_one(c,row,now):
    binding=link(c,row['id'])
    if binding is None:
        raise RuntimeError('I2C operation is missing its adapter binding')
    if row['state']=='finished' and row['resource_status']=='released' and row['outcome']!='unknown':
        return
    session=c.execute('SELECT * FROM debug_sessions WHERE id=?',(row['session_id'],)).fetchone()
    node=c.execute('SELECT * FROM devices WHERE id=?',(row['node_id'],)).fetchone()
    expected=json.loads(row['binding'])['node_boot_id']
    if node['boot_id']!=expected and session['state'] not in ('closed','lost'):
        c.execute("UPDATE debug_sessions SET state='lost',updated=? WHERE id=?",(now,session['id']))
        session=c.execute('SELECT * FROM debug_sessions WHERE id=?',(session['id'],)).fetchone()
    if binding['bus_outcome'] is not None:
        release_or_wait(c,row,now)
        return
    if session['state'] in ('closed','lost','closing','expired'):
        begin_cleanup(c,row,now,'unknown' if row['published'] else 'failed',
                      'NODE_REBOOTED' if node['boot_id']!=expected else 'CONTEXT_ENDED')
    elif not row['published'] and (node['revoked'] or not runtime.supports(node,'i2c.fixed/1') or not runtime.matches(node,json.loads(row['binding'])) or
          not {'debug_uart_v1','i2c_master_v2'}.issubset(json.loads(node['telemetry']).get('capabilities',[]))):
        begin_cleanup(c,row,now,'failed','CAPABILITY_CHANGED_BEFORE_TRANSFER')
    elif not row['published'] and now>=row['dispatch_before']:
        begin_cleanup(c,row,now,'failed' if binding['context_published'] else 'expired','BUS_DISPATCH_DEADLINE_EXCEEDED')
    elif row['published'] and row['execution_deadline'] is not None and now>=row['execution_deadline']:
        begin_cleanup(c,row,now,'unknown','I2C_CONFIRMATION_MISSING')
    else:
        return
    release_or_wait(c,ops.find(c,row['id']),now)


def submit(c,document,normalized,bench,now):
    assessment=planning.assess(c,document,bench,ops.OWNER,now)
    if assessment['status']!='supported':
        raise HTTPException(409,{'code':'OPERATION_NOT_READY','message':'I2C adapter or resources are not ready.','assessment':assessment})
    identity,sid=secrets.token_hex(12),secrets.token_hex(12)
    binding=assessment['binding'];did=binding['node_id']
    deadline=now+document['timing']['dispatch_within_ms']/1000
    expires=deadline+document['timing']['execution_timeout_ms']/1000+CLEANUP_SECONDS
    c.execute('INSERT INTO debug_sessions(id,device_id,role,boot_id,owner,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)',
              (sid,did,bench[did],binding['node_boot_id'],ops.OWNER,'opening',now,expires,now))
    c.execute('''INSERT INTO workspace_operations
        (id,principal,idempotency_key,request,request_digest,action,node_id,binding,resources,session_id,
         state,resource_status,created,updated,dispatch_before,execution_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,'accepted','held',?,?,?,?)''',
        (identity,ops.PRINCIPAL,document['idempotency_key'],normalized,hashlib.sha256(normalized.encode()).hexdigest(),
         'i2c.transfer',did,ops.dump(binding),ops.dump(assessment['resources']),sid,now,now,deadline,document['timing']['execution_timeout_ms']))
    c.execute('INSERT INTO workspace_i2c_operations(operation_id,session_id,bus_key) VALUES(?,?,?)',(identity,sid,legacy.BUS_KEY))
    ops.change(c,identity,now,'operation.accepted')
    return ops.view(ops.find(c,identity))


def before_poll(c,node_id,boot_id,snapshot,now):
    if snapshot is None:
        return
    for row in c.execute("SELECT * FROM workspace_operations WHERE node_id=? AND action='i2c.transfer' AND state='accepted' AND published=0",(node_id,)).fetchall():
        binding=link(c,row['id'])
        if binding['bus_outcome'] is not None or binding['run_id'] is not None or now>=row['dispatch_before']:
            continue
        if boot_id!=json.loads(row['binding'])['node_boot_id'] or snapshot.session!=row['session_id'] or snapshot.state!='active':
            continue
        session=c.execute('SELECT state,expires FROM debug_sessions WHERE id=?',(row['session_id'],)).fetchone()
        if session['state'] not in ('opening','active') or session['expires']<=now:
            continue
        c.execute("UPDATE debug_sessions SET state='active',updated=? WHERE id=?",(now,row['session_id']))
        request=json.loads(row['request']);operation,written,count=shape(request)
        params={'mode':'external','operation':operation,'address':request['params']['address'],'data':written,'read_len':count,
                'reply':'','speed_hz':10000,'sda':9,'scl':10,'target_address':0x42,'internal_pullup':True,
                'scan_start':8,'scan_end':0x77,'bus_key':legacy.BUS_KEY}
        rid=secrets.token_hex(12)
        c.execute('''INSERT INTO i2c_runs(id,owner,request_id,master_session,target_session,params,state,created,updated,expires,workspace_operation_id)
                     VALUES(?,?,?,?,NULL,?,'arming',?,?,?,?)''',
                  (rid,ops.OWNER,row['id'],row['session_id'],ops.dump(params),now,now,row['dispatch_before'],row['id']))
        c.execute('UPDATE workspace_i2c_operations SET run_id=?,context_published=1 WHERE operation_id=?',(rid,row['id']))
        ops.change(c,row['id'],now,'i2c.context_ready')


def after_poll(c,node_id,boot_id,snapshot,control,now):
    rows=c.execute("SELECT o.* FROM workspace_operations o JOIN workspace_i2c_operations b ON b.operation_id=o.id WHERE o.node_id=? AND (b.held=1 OR o.outcome='unknown')",(node_id,)).fetchall()
    for row in rows:
        binding=link(c,row['id'])
        if control and control.get('session')==row['session_id'] and not binding['context_published']:
            c.execute('UPDATE workspace_i2c_operations SET context_published=1 WHERE operation_id=?',(row['id'],))
            ops.change(c,row['id'],now,'i2c.context_published')
        command=control.get('i2c') if control else None
        if command and command.get('id')==binding['run_id'] and not row['published']:
            deadline=now+row['execution_ms']/1000
            ops.change(c,row['id'],now,'i2c.command_published',state='dispatched',published=1,execution_deadline=deadline)
            c.execute('UPDATE i2c_runs SET expires=? WHERE id=?',(deadline,binding['run_id']))
        row=ops.find(c,row['id']);binding=link(c,row['id'])
        report=snapshot.i2c if snapshot else None
        if (not report or not row['published'] or report.id!=binding['run_id'] or
            boot_id!=json.loads(row['binding'])['node_boot_id'] or report.state not in ('done','failed','cancelled') or json.loads(binding['bus_report'])):
            continue
        c.execute('UPDATE workspace_i2c_operations SET bus_report=? WHERE operation_id=?',(report.model_dump_json(),row['id']))
        request=json.loads(row['request']);_,_,count=shape(request)
        if report.state=='done':
            valid=len(report.data)==count*2
            outcome,code=('succeeded','I2C_MASTER_COMPLETED') if valid else ('failed','I2C_REPORT_LENGTH_MISMATCH')
        elif report.state=='cancelled':
            outcome,code='unknown','I2C_NODE_CANCELLED_UNCONFIRMED'
        else:
            outcome='failed'
            code='I2C_NACK' if 'NACK' in report.error else 'I2C_BUS_NOT_IDLE' if 'Bus not idle' in report.error else 'I2C_TIMEOUT' if report.error=='ESP_ERR_TIMEOUT' else 'I2C_DRIVER_ERROR'
        c.execute('UPDATE i2c_runs SET master_report=?,state=?,updated=? WHERE id=?',
                  (report.model_dump_json(),'completed' if outcome=='succeeded' else 'unknown' if outcome=='unknown' else 'failed',now,binding['run_id']))
        begin_cleanup(c,row,now,outcome,code)
        refresh_one(c,ops.find(c,row['id']),now)
    if control:
        session=c.execute('SELECT state FROM debug_sessions WHERE id=?',(control.get('session',''),)).fetchone()
        if session and session['state'] not in ('opening','active'):
            return None
    return control


def cancel(c,row,now):
    if row['state']=='finished':
        return ops.view(row)
    ops.change(c,row['id'],now,'cancel.requested',cancel_requested=1)
    binding=link(c,row['id'])
    if binding['bus_outcome'] is None:
        begin_cleanup(c,ops.find(c,row['id']),now,'unknown' if row['published'] else 'cancelled',
                      'I2C_CANCEL_UNCONFIRMED' if row['published'] else 'I2C_CANCELLED_BEFORE_BUS_PUBLICATION')
    refresh_one(c,ops.find(c,row['id']),now)
    return ops.view(ops.find(c,row['id']))

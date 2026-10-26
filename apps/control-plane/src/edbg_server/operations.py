"""Durable operations over conservative legacy driver adapters.

Only device polls publish hardware commands. HTTP retries never recreate an operation.
"""
import copy
import hashlib
import json
import secrets
import time

from fastapi import Depends, HTTPException, Query, Request

from edbg_server import planning, streams, runtime
from edbg_server.workspace import LOCAL_PRINCIPAL, record, reject

PRINCIPAL = LOCAL_PRINCIPAL
OWNER = 'workspace:' + PRINCIPAL
ACTIONS = ('uart.session.open', 'uart.write', 'uart.session.close', 'uart.session.renew')


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def normalize(document):
    result = copy.deepcopy(document)
    result.pop('idempotency_key')
    if result['action'] == 'uart.write':
        result['params']['data_hex'] = result['params']['data_hex'].lower()
    elif result['action']=='i2c.transfer':
        for message in result['params']['messages']:
            if 'write_hex' in message:message['write_hex']=message['write_hex'].lower()
    return dump(result)


def view(row):
    return {'api_version': 'edbg.operation/v0', **{k: row[k] for k in (
        'id', 'action', 'state', 'outcome', 'resource_status', 'revision', 'created', 'updated',
        'node_id', 'session_id', 'request_digest', 'dispatch_before', 'execution_deadline')},
        'normalization': 'strict-json-update/1' if row['action']=='node.update' else 'strict-json-i2c/1' if row['action']=='i2c.transfer' else 'strict-json-uart/1', 'cancel_requested': bool(row['cancel_requested']),
        'compatibility': {'level': 'agent_image_identity' if row['action']=='node.update' else 'legacy_i2c_once' if row['action']=='i2c.transfer' else 'legacy_uart_once', 'write_delivery': 'single_publication_no_retry',
                          'timeout_semantics': 'server_confirmation_window', 'node_execution_budget_enforced': False,
                          'node_local_lease_ms_max': None if row['action']=='node.update' else 10000},
        'binding': json.loads(row['binding']), 'resources': json.loads(row['resources']),
        'result': json.loads(row['result']), 'evidence': json.loads(row['evidence'])}


def find(c, identity):
    row = c.execute('SELECT * FROM workspace_operations WHERE id=? AND principal=?', (identity, PRINCIPAL)).fetchone()
    if row is None:
        reject('OPERATION_NOT_FOUND', 'Operation was not found.', 404)
    return row


def change(c, identity, now, kind, **updates):
    row = find(c, identity)
    if updates and all(row[key] == value for key, value in updates.items()):
        return
    revision = row['revision'] + 1
    assignments = ','.join(key + '=?' for key in updates)
    prefix = assignments + ',' if assignments else ''
    c.execute(f'UPDATE workspace_operations SET {prefix}updated=?,revision=? WHERE id=?',
              (*updates.values(), now, revision, identity))
    current = find(c, identity)
    event = {'kind': kind, 'state': current['state'], 'outcome': current['outcome'],
             'resource_status': current['resource_status'], 'cancel_requested': bool(current['cancel_requested']),
             'result': json.loads(current['result']), 'evidence': json.loads(current['evidence'])}
    c.execute('INSERT INTO workspace_operation_events VALUES(?,?,?,?)', (identity, revision, now, dump(event)))


def finish(c, row, now, outcome, resource, code, level, effect):
    result = {'code': code, 'session_id': row['session_id'], 'effect': effect}
    if row['action'] == 'uart.session.open':
        result['stream_id'] = streams.identity(row['session_id'])
    if row['action'] == 'uart.write':
        result['requested_bytes'] = len(json.loads(row['request'])['params']['data_hex']) // 2
        result['confirmed_bytes'] = result['requested_bytes'] if outcome == 'succeeded' else None
    if row['state'] == 'finished' and row['outcome'] == outcome and row['result'] == dump(result) and row['resource_status'] == resource:
        return
    evidence = json.loads(row['evidence'])
    if not evidence or evidence[-1].get('code') != code:
        evidence.append({'code': code, 'level': level, 'observed_at': now, 'boot_id': json.loads(row['binding'])['node_boot_id']})
    change(c, row['id'], now, 'result.reconciled' if row['state'] == 'finished' else 'operation.finished',
           state='finished', outcome=outcome, resource_status=resource, result=dump(result), evidence=dump(evidence))


def refresh(c, now, node_id=None):
    """Persist timeout/evidence observations; never publish a command here."""
    pending = "(o.state!='finished' OR o.resource_status!='released' OR (o.outcome='unknown' AND EXISTS(SELECT 1 FROM debug_tx t WHERE t.id=o.tx_id AND t.state IN ('sent','failed'))))"
    where, args = (' WHERE ' + pending + ' AND o.node_id=?', (node_id,)) if node_id else (' WHERE ' + pending, ())
    rows = c.execute('SELECT o.* FROM workspace_operations o' + where + ' ORDER BY o.created,o.id', args).fetchall()
    for initial in rows:
        row = find(c, initial['id'])
        if row['action']=='node.update':
            from edbg_server import node_updates
            node_updates.refresh_one(c,row,now)
            continue
        if row['action']=='i2c.transfer':
            from edbg_server import i2c_operations
            i2c_operations.refresh_one(c,row,now)
            continue
        session = c.execute('SELECT * FROM debug_sessions WHERE id=?', (row['session_id'],)).fetchone()
        node = c.execute('SELECT * FROM devices WHERE id=?', (row['node_id'],)).fetchone()
        binding = json.loads(row['binding'])
        rebooted = node is not None and node['boot_id'] != binding['node_boot_id']
        if rebooted and session['state'] not in ('closed', 'lost'):
            c.execute("UPDATE debug_sessions SET state='lost',updated=? WHERE id=?", (now, session['id']))
            c.execute("UPDATE debug_tx SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'unknown' END WHERE session_id=? AND state IN ('queued','dispatched')", (session['id'],))
            session = c.execute('SELECT * FROM debug_sessions WHERE id=?', (session['id'],)).fetchone()
        released = session['state'] in ('closed', 'lost')
        if row['state'] == 'finished' and row['outcome'] != 'unknown':
            if released and row['resource_status'] != 'released':
                change(c, row['id'], now, 'resources.released', resource_status='released')
            continue
        action = row['action']
        if action == 'uart.write':
            tx = c.execute('SELECT * FROM debug_tx WHERE id=?', (row['tx_id'],)).fetchone()
            if tx['state'] in ('sent', 'failed'):
                finish(c, row, now, 'succeeded' if tx['state'] == 'sent' else 'failed', 'released',
                       'UART_DRIVER_COMPLETED' if tx['state'] == 'sent' else 'UART_DRIVER_ERROR', 'driver_report',
                       'confirmed' if tx['state'] == 'sent' else 'possible')
                continue
        if action == 'uart.session.close' and released:
            finish(c, row, now, 'succeeded', 'released', 'NODE_REBOOTED' if rebooted else 'SESSION_RELEASE_CONFIRMED', 'node_report', 'confirmed')
            continue
        if action == 'uart.session.open' and session['state'] == 'active' and not row['cancel_requested'] and row['state'] != 'finished':
            finish(c, row, now, 'succeeded', 'held', 'SESSION_ACTIVE_CONFIRMED', 'node_report', 'confirmed')
            continue
        if released:
            outcome = 'cancelled' if row['cancel_requested'] and action == 'uart.session.open' else 'unknown' if row['published'] else 'failed'
            finish(c, row, now, outcome, 'released', 'NODE_REBOOTED' if rebooted else 'SESSION_ENDED', 'node_report', 'possible' if row['published'] else 'none')
            continue
        if row['state'] == 'finished':
            continue
        if not row['published'] and now >= row['dispatch_before']:
            if action == 'uart.session.open':
                c.execute("UPDATE debug_sessions SET state='closed',updated=? WHERE id=?", (now, session['id']))
            elif action == 'uart.write':
                c.execute("UPDATE debug_tx SET state='expired' WHERE id=? AND state='queued'", (row['tx_id'],))
            finish(c, row, now, 'unknown' if action == 'uart.session.close' else 'expired',
                   'unconfirmed' if action == 'uart.session.close' else 'released',
                   'DISPATCH_DEADLINE_EXCEEDED', 'server_ledger', 'none')
            continue
        if row['published'] and row['execution_deadline'] is not None and now >= row['execution_deadline']:
            if action == 'uart.session.open':
                c.execute("UPDATE debug_sessions SET state='closing',updated=? WHERE id=? AND state IN ('opening','active')", (now, session['id']))
            elif action == 'uart.write':
                c.execute("UPDATE debug_tx SET state='unknown' WHERE id=? AND state IN ('queued','dispatched')", (row['tx_id'],))
            finish(c, row, now, 'unknown', 'unconfirmed', 'EXECUTION_CONFIRMATION_MISSING', 'server_ledger', 'possible')
            continue
        if action == 'uart.write' and (session['state'] != 'active' or session['expires'] <= now):
            c.execute("UPDATE debug_tx SET state=? WHERE id=? AND state IN ('queued','dispatched')", ('unknown' if row['published'] else 'cancelled', row['tx_id']))
            finish(c, row, now, 'unknown' if row['published'] else 'failed', 'unconfirmed' if row['published'] else 'released',
                   'SESSION_NO_LONGER_READY', 'server_ledger', 'possible' if row['published'] else 'none')
    for affected_node in {row['node_id'] for row in rows}:
        streams.synchronize(c, affected_node, now)


def submit(c, document, bench, now):
    normalized = normalize(document)
    existing = c.execute('SELECT * FROM workspace_operations WHERE principal=? AND idempotency_key=?', (PRINCIPAL, document['idempotency_key'])).fetchone()
    if existing:
        if existing['request'] != normalized:
            reject('IDEMPOTENCY_CONFLICT', 'This key already identifies a different intent.')
        refresh(c, now, existing['node_id'])
        return view(find(c, existing['id']))
    action = document['action']
    if action=='node.update':
        from edbg_server import node_updates
        refresh(c,now)
        return node_updates.submit(c,document,normalized,bench,now)
    if action=='i2c.transfer':
        from edbg_server import i2c_operations
        refresh(c,now)
        return i2c_operations.submit(c,document,normalized,bench,now)
    if action not in ACTIONS:
        reject('ACTION_NOT_IMPLEMENTED', 'Executable subset is managed UART, finite I2C and compatible node update operations.', 422)
    refresh(c, now)
    identity, tx_id, renewal = secrets.token_hex(12), None, None
    if action == 'uart.session.close':
        sid = document['params']['session_id']
        managed = c.execute('SELECT * FROM workspace_uart_sessions WHERE session_id=? AND principal=?', (sid, PRINCIPAL)).fetchone()
        if not managed or managed['connection_id'] != document['connection']['id'] or managed['connection_revision'] != document['connection']['revision']:
            reject('SESSION_BINDING_MISMATCH', 'Close requires the original managed session and connection revision.')
        if document['timing']['execution_timeout_ms'] > 30000:
            reject('OPERATION_NOT_READY', 'UART close confirmation budget is at most 30 seconds.', 422)
        binding = json.loads(managed['binding'])
        resources = json.loads(c.execute("SELECT resources FROM workspace_operations WHERE session_id=? AND action='uart.session.open'", (sid,)).fetchone()[0])
        c.execute("UPDATE debug_sessions SET state='closing',updated=? WHERE id=? AND state IN ('opening','active','expired')", (now, sid))
        c.execute("UPDATE debug_tx SET state='cancelled' WHERE session_id=? AND state='queued'", (sid,))
    else:
        assessment = planning.assess(c, document, bench, OWNER, now)
        if assessment['status'] != 'supported':
            if action=='uart.session.renew':
                codes={reason['code'] for reason in assessment['reasons']}
                if 'LEASE_REVISION_CONFLICT' in codes:
                    reject('LEASE_REVISION_CONFLICT','Lease changed; read its current revision before creating another renewal.')
                if 'LEASE_NOT_EXTENDED' in codes:
                    reject('LEASE_NOT_EXTENDED','Requested duration must extend the current server deadline.')
            raise HTTPException(409, {'code': 'OPERATION_NOT_READY', 'message': 'Current binding, capability or control context is not ready.', 'assessment': assessment})
        binding, resources = assessment['binding'], assessment['resources']
        if action == 'uart.session.open':
            sid = secrets.token_hex(12)
            c.execute('INSERT INTO debug_sessions(id,device_id,role,boot_id,owner,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                      (sid, binding['node_id'], bench[binding['node_id']], binding['node_boot_id'], OWNER, 'opening', now,
                       now + document['params']['lease_duration_ms'] / 1000, now))
            c.execute('INSERT INTO workspace_uart_sessions(session_id,principal,connection_id,connection_revision,binding) VALUES(?,?,?,?,?)',
                      (sid, PRINCIPAL, document['connection']['id'], document['connection']['revision'], dump(binding)))
            streams.create(c, sid, binding, PRINCIPAL, now=now)
        elif action == 'uart.session.renew':
            sid = document['params']['session_id']
            session = c.execute('SELECT expires FROM debug_sessions WHERE id=?', (sid,)).fetchone()
            expected = document['params']['expected_lease_revision']
            expires = now + document['params']['lease_duration_ms']/1000
            changed = c.execute('UPDATE workspace_uart_sessions SET lease_revision=lease_revision+1 WHERE session_id=? AND principal=? AND lease_revision=?',
                                (sid, PRINCIPAL, expected)).rowcount
            if changed != 1:
                reject('LEASE_REVISION_CONFLICT', 'Read the current lease revision before submitting a new renewal.')
            c.execute('UPDATE debug_sessions SET expires=?,updated=? WHERE id=?', (expires, now, sid))
            renewal = {'code':'SERVER_LEASE_EXTENDED','session_id':sid,'previous_lease_revision':expected,'lease_revision':expected+1,
                       'previous_expires_at':session['expires'],'expires_at':expires,'effect':'server_policy',
                       'confirmation':'server_policy_only','node_revision_acknowledged':False,'session_resources_held':True}
        else:
            sid = document['params']['session_id']
            if c.execute("SELECT 1 FROM workspace_operations WHERE session_id=? AND action='uart.write' AND outcome='unknown'", (sid,)).fetchone():
                reject('PREVIOUS_RESULT_UNKNOWN', 'Resolve the original write or close this context before another write.')
            tx_id = secrets.token_hex(12)
            c.execute('INSERT INTO debug_tx(id,session_id,request_id,data,state,created,expires) VALUES(?,?,?,?,?,?,?)',
                      (tx_id, sid, identity, document['params']['data_hex'].lower(), 'queued', now,
                       now + document['timing']['dispatch_within_ms'] / 1000))
    c.execute('''INSERT INTO workspace_operations
        (id,principal,idempotency_key,request,request_digest,action,node_id,binding,resources,session_id,tx_id,
         state,resource_status,created,updated,dispatch_before,execution_ms)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted','held',?,?,?,?)''',
        (identity, PRINCIPAL, document['idempotency_key'], normalized, hashlib.sha256(normalized.encode()).hexdigest(), action,
         binding['node_id'], dump(binding), dump(resources), sid, tx_id, now, now,
         now + document['timing']['dispatch_within_ms'] / 1000, document['timing']['execution_timeout_ms']))
    change(c, identity, now, 'operation.accepted')
    if renewal:
        change(c, identity, now, 'lease.policy_extended', state='finished', outcome='succeeded',resource_status='released',
               result=dump(renewal), evidence=dump([{'code':'SERVER_LEASE_EXTENDED','level':'server_policy',
                    'observed_at':now,'boot_id':binding['node_boot_id']}]))
    refresh(c, now, binding['node_id'])
    return view(find(c, identity))


def before_poll(c, node_id, now):
    refresh(c, now, node_id)
    # Never publish a pending intent if agent facts changed within the same boot.
    node = c.execute('SELECT * FROM devices WHERE id=?', (node_id,)).fetchone()
    eligible = node and not node['revoked'] and runtime.supports(node,'uart.fixed/1') and 'debug_uart_v1' in json.loads(node['telemetry']).get('capabilities', [])
    for row in c.execute("SELECT * FROM workspace_operations WHERE node_id=? AND published=0 AND state!='finished' AND action NOT IN ('uart.session.close','i2c.transfer','node.update')", (node_id,)).fetchall():
        if not eligible or not runtime.matches(node,json.loads(row['binding'])):
            if row['tx_id']:
                c.execute("UPDATE debug_tx SET state='cancelled' WHERE id=? AND state='queued'", (row['tx_id'],))
            if row['action'] == 'uart.session.open':
                c.execute("UPDATE debug_sessions SET state='closed',updated=? WHERE id=?", (now, row['session_id']))
            finish(c, row, now, 'failed', 'released', 'CAPABILITY_CHANGED_BEFORE_DISPATCH', 'server_ledger', 'none')


def after_poll(c, node_id, boot_id, snapshot, control, now):
    for row in c.execute("SELECT * FROM workspace_operations WHERE node_id=? AND published=0 AND state!='finished' AND action NOT IN ('i2c.transfer','node.update')", (node_id,)).fetchall():
        published = (row['action'] == 'uart.session.open' and control and control.get('session') == row['session_id'])
        published = published or (row['action'] == 'uart.write' and control and control.get('tx', {}).get('id') == row['tx_id'])
        published = published or row['action'] == 'uart.session.close'
        if published:
            deadline = now + row['execution_ms'] / 1000
            change(c, row['id'], now, 'command.published', state='dispatched', published=1, execution_deadline=deadline)
            if row['tx_id']:
                c.execute('UPDATE debug_tx SET expires=? WHERE id=?', (deadline, row['tx_id']))
    # Raw late confirmations are checked against the immutable operation/boot/session identity.
    if snapshot and snapshot.tx_id:
        row = c.execute('SELECT * FROM workspace_operations WHERE node_id=? AND tx_id=? AND session_id=? AND published=1',
                        (node_id, snapshot.tx_id, snapshot.session)).fetchone()
        if row and json.loads(row['binding'])['node_boot_id'] == boot_id:
            c.execute("UPDATE debug_tx SET state=?,detail=? WHERE id=? AND state IN ('dispatched','unknown')",
                      ('failed' if snapshot.tx_error else 'sent', snapshot.tx_error or 'UART driver completion reported', row['tx_id']))
    refresh(c, now, node_id)


def cancel(c, identity, now):
    refresh(c, now)
    row = find(c, identity)
    if row['action']=='node.update':
        from edbg_server import node_updates
        return node_updates.cancel(c,row,now)
    if row['action']=='i2c.transfer':
        from edbg_server import i2c_operations
        return i2c_operations.cancel(c,row,now)
    if row['state'] == 'finished':
        return view(row)
    if row['action'] == 'uart.session.close':
        reject('CANCELLATION_UNSUPPORTED', 'A session release intent cannot be reversed.', 422)
    change(c, identity, now, 'cancel.requested', cancel_requested=1)
    if row['action'] == 'uart.session.open':
        c.execute("UPDATE debug_sessions SET state=?,updated=? WHERE id=?", ('closing' if row['published'] else 'closed', now, row['session_id']))
    if not row['published']:
        if row['tx_id']:
            c.execute("UPDATE debug_tx SET state='cancelled' WHERE id=? AND state='queued'", (row['tx_id'],))
        finish(c, find(c, identity), now, 'cancelled', 'released', 'CANCELLED_BEFORE_PUBLICATION', 'server_ledger', 'none')
    streams.synchronize(c, row['node_id'], now)
    return view(find(c, identity))


async def read_document(request):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > planning.MAX_REQUEST_BYTES:
            reject('REQUEST_TOO_LARGE', 'Operation request exceeds 256 KiB.', 413)
        raw.extend(chunk)
    return planning.parse_request(bytes(raw))


def register(app, db, admin, bench, audit):
    @app.get('/api/workspace/sessions', dependencies=[Depends(admin)])
    def sessions(node: str = '', before: str = '', limit: int = Query(default=50,ge=1,le=200)):
        with db() as c:
            c.execute('BEGIN')
            where,args='w.principal=?',[PRINCIPAL]
            if node:
                where+=' AND s.device_id=?';args.append(node)
            if before:
                cursor=managed_session(c,before)
                where+=' AND (s.created,s.id)<(?,?)';args.extend((cursor['created'],cursor['id']))
            rows=c.execute('SELECT s.id FROM workspace_uart_sessions w JOIN debug_sessions s ON s.id=w.session_id WHERE '+where+' ORDER BY s.created DESC,s.id DESC LIMIT ?',(*args,limit+1)).fetchall()
            return {'items':[session_view(c,r['id']) for r in rows[:limit]],'next_before':rows[limit-1]['id'] if len(rows)>limit else None}

    @app.get('/api/workspace/sessions/{identity}', dependencies=[Depends(admin)])
    def session(identity: str):
        with db() as c:
            c.execute('BEGIN')
            return session_view(c,identity)

    @app.post('/api/workspace/operations', dependencies=[Depends(admin)])
    async def create(request: Request):
        document = await read_document(request)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            result = submit(c, document, bench, time.time())
            audit(c, 'workspace_operation_submit', result['id'])
            return result

    @app.get('/api/workspace/operations', dependencies=[Depends(admin)])
    def listing(node: str = '', before: str = '', limit: int = Query(default=50, ge=1, le=200)):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            refresh(c, time.time())
            where, args = 'principal=?', [PRINCIPAL]
            if node:
                where += ' AND node_id=?'; args.append(node)
            if before:
                cursor = find(c, before)
                where += ' AND (created,id)<(?,?)'; args.extend((cursor['created'], cursor['id']))
            rows = c.execute('SELECT * FROM workspace_operations WHERE ' + where + ' ORDER BY created DESC,id DESC LIMIT ?', (*args, limit+1)).fetchall()
            return {'items': [view(r) for r in rows[:limit]], 'next_before': rows[limit-1]['id'] if len(rows)>limit else None}

    @app.get('/api/workspace/operations/{identity}', dependencies=[Depends(admin)])
    def get(identity: str):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = find(c, identity)
            refresh(c, time.time(), row['node_id'])
            return view(find(c, identity))

    @app.post('/api/workspace/operations/{identity}/cancel', dependencies=[Depends(admin)])
    def cancellation(identity: str):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            result = cancel(c, identity, time.time())
            audit(c, 'workspace_operation_cancel', identity)
            return result

    @app.get('/api/workspace/operations/{identity}/events', dependencies=[Depends(admin)])
    def events(identity: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=200)):
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = find(c, identity)
            refresh(c, time.time(), row['node_id'])
            rows = c.execute('SELECT * FROM workspace_operation_events WHERE operation_id=? AND seq>? ORDER BY seq LIMIT ?', (identity, after, limit+1)).fetchall()
            items = [{'seq': r['seq'], 'created': r['created'], **json.loads(r['document'])} for r in rows[:limit]]
            return {'operation_id': identity, 'items': items, 'next_after': items[-1]['seq'] if items else after, 'has_more': len(rows)>limit}


def managed_session(c, identity):
    row=c.execute('''SELECT s.*,w.binding,w.lease_revision FROM workspace_uart_sessions w
                    JOIN debug_sessions s ON s.id=w.session_id WHERE s.id=? AND w.principal=?''',(identity,PRINCIPAL)).fetchone()
    if row is None:
        reject('SESSION_NOT_FOUND','Managed session was not found.',404)
    return row


def session_view(c, identity):
    row=managed_session(c,identity);now=time.time()
    node=c.execute('SELECT boot_id,last_seen,revoked FROM devices WHERE id=?',(row['device_id'],)).fetchone()
    return {'api_version':'edbg.session/v0','kind':'uart','id':identity,'node_id':row['device_id'],'boot_id':row['boot_id'],
            'state':row['state'],'created':row['created'],'binding':json.loads(row['binding']),
            'stream_id':streams.identity(identity),'lease':{'revision':row['lease_revision'],'expires_at':row['expires'],
                'remaining_ms':max(0,int((row['expires']-now)*1000)),'expired':row['expires']<=now,
                'scope':'server_policy','node_revision_acknowledged':False},
            'node':{'current_boot_id':node['boot_id'],'last_seen':node['last_seen'],
                'control_heartbeat_fresh':bool(not node['revoked'] and 0<=now-node['last_seen']<8)},
            'last_context_snapshot':json.loads(row['snapshot'])}

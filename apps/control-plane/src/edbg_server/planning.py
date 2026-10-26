"""Read-only compatibility assessment. This module never dispatches or reserves resources."""
import hashlib
import json
import time

from fastapi import Depends, HTTPException, Request
from jsonschema import Draft202012Validator, ValidationError

from edbg_server.paths import WORKSPACE_ROOT
from edbg_server import artifacts, runtime
from edbg_server.workspace import record, LOCAL_PRINCIPAL

MAX_REQUEST_BYTES = 262144
SCHEMA_PATH = WORKSPACE_ROOT / 'contracts/drafts/operation-v0/request.schema.json'
VALIDATOR = Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding='utf-8')))


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def reject_number(value):
    raise ValueError('Non-finite JSON number')


def strict_numbers(value):
    if isinstance(value, float):
        raise ValueError('Floating-point request value')
    if isinstance(value, dict):
        for child in value.values():
            strict_numbers(child)
    elif isinstance(value, list):
        for child in value:
            strict_numbers(child)


def parse_request(raw):
    try:
        document = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_number)
        strict_numbers(document)
        VALIDATOR.validate(document)
        return document
    except (ValueError, ValidationError, RecursionError):
        raise HTTPException(422, {'code': 'INVALID_OPERATION_REQUEST',
                                 'message': 'Request does not satisfy the operation draft schema and strict JSON rules.'})


def assess(c, request, bench, owner, now=None):
    now = time.time() if now is None else now
    response = {'api_version': 'edbg.operation.plan/v0', 'status': 'supported', 'checked_at': now,
                'submission_available': False, 'assessment_scope': 'legacy_adapter_precheck',
                'reserved': False, 'reasons': [], 'warnings': [], 'resources': [], 'binding': None,
                'facts': [{'source': 'deployment_configuration', 'scope': 'wiring_and_target', 'measured': False}]}

    def reason(status, code, message):
        response['reasons'].append({'category': status, 'code': code, 'message': message})

    def finish():
        categories = {r['category'] for r in response['reasons']}
        response['status'] = next((s for s in ('unsupported', 'blocked', 'unknown') if s in categories), 'supported')
        response['submission_available'] = response['status'] == 'supported' and request['action'] in ('uart.session.open', 'uart.write', 'uart.session.close','uart.session.renew','i2c.transfer','node.update')
        return response

    ref = request['connection']
    connection = record(c, 'connection', ref['id'])
    if connection is None:
        reason('blocked', 'CONNECTION_NOT_FOUND', 'Connection does not exist.')
        return finish()
    if connection['revision'] != ref['revision']:
        reason('blocked', 'CONNECTION_REVISION_MISMATCH', 'Use the current connection revision; historical revisions are inspection-only.')
        return finish()
    target_ref = connection['target']
    target = record(c, 'target', target_ref['id'], target_ref['revision'])
    node = c.execute('SELECT * FROM devices WHERE id=?', (connection['node_id'],)).fetchone()
    response['binding'] = {'connection': ref, 'target': target_ref, 'node_id': connection['node_id'],
                           'interface': connection['interface'], 'adapter_id': connection['adapter_id'],
                           'adapter_revision': 'esp32s3-fixed/1', 'node_boot_id': node['boot_id'] if node else None}
    if not connection['enabled']:
        reason('blocked', 'CONNECTION_DISABLED', 'Connection is disabled.')
    if not target:
        reason('blocked', 'TARGET_NOT_FOUND', 'Pinned target revision does not exist.')
    if not node or node['revoked']:
        reason('blocked', 'NODE_UNAVAILABLE', 'Node is missing or revoked.')
        return finish()
    did, action, params = node['id'], request['action'], request['params']
    response['facts'].append({'source': 'node_report', 'last_seen': node['last_seen'], 'boot_id': node['boot_id']})
    if now - node['last_seen'] > 8 or node['last_seen'] > now + 1 or not node['boot_id']:
        reason('blocked', 'NODE_OFFLINE', 'Node heartbeat or boot context is not current.')
    expected_boot = request.get('preconditions', {}).get('node_boot_id')
    if expected_boot is not None and expected_boot != node['boot_id']:
        reason('blocked', 'BOOT_CONTEXT_CHANGED', 'Node no longer has the requested boot identity.')
    if connection['adapter_id'] != 'esp32s3-gpio-v1':
        reason('unknown', 'ADAPTER_NOT_INSTALLED', 'No installed assessor can interpret this adapter.')
        response['binding']['adapter_revision'] = None
        return finish()
    if did not in bench and (action.startswith('uart.') or action == 'i2c.transfer'):
        reason('unsupported', 'FIRMWARE_IDENTITY_RESTRICTED', 'Current debug firmware permits only its original provisioned identities.')
    agent=runtime.parse(node)
    response['binding']['agent']=runtime.snapshot(node)
    if not agent or agent.family!='esp32s3':
        reason('unknown','AGENT_PROTOCOL_UNSUPPORTED','This adapter requires an edbg.agent/v1 ESP32-S3 runtime report.')
    elif agent.health!='valid' or agent.image is None or agent.image.project!='fleet_agent':
        reason('blocked','AGENT_NOT_HEALTHY','Agent has not confirmed its running image and healthy boot.')
    interfaces = {'uart': 'uart1', 'i2c': 'i2c0', 'maintenance': 'agent'}
    interface = connection['interface']
    expected_kind = 'uart' if action.startswith('uart.') else 'i2c' if action == 'i2c.transfer' else 'maintenance'
    if interface['kind'] != expected_kind or interface['id'] != interfaces[interface['kind']]:
        reason('unsupported', 'INTERFACE_ACTION_MISMATCH', 'Action is not mapped to this legacy interface.')
    if action == 'target.flash':
        reason('unsupported', 'FLASH_PROVIDER_UNAVAILABLE', 'Current ESP32 agent cannot program an external target.')
        return finish()
    if action == 'node.update':
        if not runtime.supports(node,'node.update/1') or not runtime.supports(node,'runtime.image/1'):
            reason('unsupported','RUNTIME_IMAGE_CONFIRMATION_UNAVAILABLE','Node must provide image identity and node.update/1.')
        image=artifacts.lookup(c,params['image']['artifact_id'])
        if image is None:
            reason('blocked','ARTIFACT_NOT_FOUND','Image is not registered in the node firmware catalog.')
        elif image['sha256']!=params['image']['sha256']:
            reason('blocked','ARTIFACT_DIGEST_MISMATCH','Requested digest differs from the immutable registered image.')
        elif image['withdrawn']:
            reason('blocked','ARTIFACT_WITHDRAWN','Image was withdrawn and cannot be published.')
        elif not artifacts.check_file(image):
            reason('blocked','ARTIFACT_INTEGRITY_FAILED','Stored bytes do not match the registered image.')
        else:
            response['binding']['image']={'artifact_id':image['id'],'sha256':image['sha256'],'version':image['version'],'size':image['size']}
        response['binding']['confirmation']=params['confirmation']
        response['binding']['source_partition']=node['partition']
        response['resources']=[{'id':f'node:{did}:maintenance','mode':'exclusive'},{'id':f'node:{did}:legacy-debug','mode':'exclusive'}]
        response['warnings'].extend(['Image identity is authenticated node evidence, not hardware-rooted remote attestation.',
            'OTA publishes once without retry; confirmation timeout keeps maintenance resources held.',
            'Image structural/checksum verification does not verify a publisher signature.'])
        if request['timing']['dispatch_within_ms']>30000 or request['timing']['execution_timeout_ms']>300000:
            reason('unsupported','UPDATE_BUDGET_UNSUPPORTED','Compatibility OTA dispatch is at most 30 seconds and confirmation at most 300 seconds.')
        if c.execute("SELECT 1 FROM jobs WHERE device_id=? AND state IN ('queued','dispatched','running','rebooting')",(did,)).fetchone():
            reason('blocked','NODE_JOB_ACTIVE','A legacy or managed job is unfinished.')
        if c.execute('SELECT 1 FROM workspace_update_operations WHERE node_id=? AND held=1',(did,)).fetchone():
            reason('blocked','NODE_MAINTENANCE_HELD','Maintenance completion remains unconfirmed.')
        if c.execute("SELECT 1 FROM debug_sessions WHERE device_id=? AND state IN ('opening','active','closing','expired')",(did,)).fetchone():
            reason('blocked','DEBUG_CONTEXT_OCCUPIED','A debug context still holds node resources.')
        return finish()
    telemetry = json.loads(node['telemetry'])
    capability = 'i2c_master_v2' if action == 'i2c.transfer' else 'debug_uart_v1'
    if capability not in telemetry.get('capabilities', []):
        reason('unsupported', 'CAPABILITY_NOT_REPORTED', 'Required capability was not reported by this node.')
    if not runtime.supports(node,'i2c.fixed/1' if action=='i2c.transfer' else 'uart.fixed/1'):
        reason('unsupported','PROVIDER_UNAVAILABLE','Required versioned driver provider was not reported by the node.')
    response['binding']['capability_report_sha256'] = hashlib.sha256(node['telemetry'].encode()).hexdigest()
    # Conservatively mirror the legacy adapter, including bundled UART and global I2C lock.
    resources = {f'node:{did}:legacy-debug', f'node:{did}:uart1', f'node:{did}:gpio4', f'node:{did}:gpio5'}
    if bench.get(did) == 'probe':
        resources.update((f'node:{did}:uart2', f'node:{did}:gpio8'))
    response['warnings'].extend(['Snapshot only; no reservation or hardware observation was performed.',
                                 'Legacy debug context also enables UART; resources are conservatively exclusive.'])
    if action == 'uart.session.open':
        fixed = {'baud_rate': 115200, 'data_bits': 8, 'parity': 'none', 'stop_bits': '1', 'flow_control': 'none'}
        if any(params[k] != value for k, value in fixed.items()) or params['lease_duration_ms'] > 300000:
            reason('unsupported', 'UART_PARAMETERS_UNSUPPORTED', 'Legacy UART supports 115200 8N1, no flow control, and at most 300 seconds.')
        response['warnings'].append('Managed lease is bounded from acceptance time and is not automatically renewed.')
    if action.startswith('uart.') and request['timing']['execution_timeout_ms'] > (10000 if action == 'uart.write' else 30000):
        reason('unsupported', 'EXECUTION_BUDGET_UNSUPPORTED', 'Managed UART budget is at most 10 seconds for writes, 30 seconds for open/close.')
    if action.startswith('uart.'):
        response['warnings'].append('Legacy execution_timeout_ms is a server confirmation window; a per-operation node budget is not transmitted.')
    if action == 'i2c.transfer':
        if 'debug_uart_v1' not in telemetry.get('capabilities',[]):
            reason('unsupported','PARENT_CONTEXT_CAPABILITY_MISSING','Legacy I2C also requires its debug/UART context capability.')
        bus = connection['physical_bus_id']
        resources.update((f'bus:{bus}', 'adapter:legacy-i2c-global', f'node:{did}:i2c0', f'node:{did}:gpio9', f'node:{did}:gpio10'))
        messages = params['messages']
        shapes = ['write' if 'write_hex' in m else 'read' for m in messages]
        if params['speed_hz'] != 10000 or shapes not in (['write'], ['read'], ['write', 'read']) or any(len(m.get('write_hex', '')) > 128 or m.get('read_length', 0) > 64 for m in messages):
            reason('unsupported', 'I2C_PARAMETERS_UNSUPPORTED', 'Legacy master supports 10 kHz, up to 64 bytes per direction, and write/read/write-read shapes.')
        response['warnings'].append('Wiring, pull-ups and target presence are declarations, not electrical measurements or ACK observations.')
        response['warnings'].append('Finite I2C creates and closes an owned debug context; completion waits for context release.')
        if request['timing']['dispatch_within_ms']>10000 or request['timing']['execution_timeout_ms']>10000:
            reason('unsupported','I2C_BUDGET_UNSUPPORTED','Compatibility I2C dispatch and confirmation windows are at most 10 seconds each.')
        if c.execute("SELECT 1 FROM i2c_runs WHERE state IN ('arming','running','verifying') LIMIT 1").fetchone():
            reason('blocked', 'I2C_ADAPTER_BUSY', 'Legacy global I2C resource has an unfinished operation.')
        if c.execute('SELECT 1 FROM workspace_i2c_operations WHERE held=1 LIMIT 1').fetchone():
            reason('blocked','I2C_RELEASE_UNCONFIRMED','Managed I2C still holds the adapter until context release is confirmed.')
        if c.execute("""SELECT 1 FROM i2c_runs r JOIN debug_sessions s ON s.id=r.master_session
            WHERE r.workspace_operation_id IS NULL AND r.state='unknown' AND r.dispatched=1
            AND s.state IN ('opening','active','closing','expired') LIMIT 1""").fetchone():
            reason('blocked','LEGACY_I2C_RELEASE_UNCONFIRMED','An unknown legacy transaction still has an unreleased context.')
    response['resources'] = [{'id': identity, 'mode': 'exclusive'} for identity in sorted(resources)]
    if c.execute("SELECT 1 FROM jobs WHERE device_id=? AND state IN ('queued','dispatched','running','rebooting')", (did,)).fetchone():
        reason('blocked', 'NODE_JOB_ACTIVE', 'A legacy node job is unfinished.')
    if c.execute('SELECT 1 FROM workspace_update_operations WHERE node_id=? AND held=1',(did,)).fetchone():
        reason('blocked','NODE_MAINTENANCE_HELD','Managed node maintenance has not confirmed completion.')
    live = c.execute("SELECT * FROM debug_sessions WHERE device_id=? AND state IN ('opening','active','closing','expired')", (did,)).fetchone()
    if action in ('uart.write', 'uart.session.close','uart.session.renew'):
        managed = c.execute('SELECT * FROM workspace_uart_sessions WHERE session_id=?', (params['session_id'],)).fetchone()
        if not managed:
            reason('unknown', 'LEGACY_SESSION_BINDING_UNAVAILABLE', 'Legacy sessions have no connection revision snapshot; only node and boot membership can be checked.')
        elif managed['principal'] != LOCAL_PRINCIPAL or managed['connection_id'] != ref['id'] or managed['connection_revision'] != ref['revision']:
            reason('blocked', 'SESSION_BINDING_MISMATCH', 'Managed session belongs to another connection revision or principal.')
        session = c.execute('SELECT * FROM debug_sessions WHERE id=?', (params['session_id'],)).fetchone()
        if not session or session['device_id'] != did:
            reason('blocked', 'SESSION_BINDING_MISMATCH', 'Session does not belong to this connection node.')
        elif session['owner'] != owner and not (managed and managed['principal'] == LOCAL_PRINCIPAL):
            reason('blocked', 'SESSION_CONTROL_NOT_HELD', 'Current login session does not own this legacy control context.')
        elif session['state'] != 'active' or session['expires'] <= now or session['boot_id'] != node['boot_id']:
            reason('blocked', 'SESSION_NOT_READY', 'Session is inactive, expired or from another boot.')
        if action == 'uart.session.renew' and managed and session:
            if managed['lease_revision'] != params['expected_lease_revision']:
                reason('blocked','LEASE_REVISION_CONFLICT','The expected server lease revision is stale.')
            if now+params['lease_duration_ms']/1000 <= session['expires']:
                reason('blocked','LEASE_NOT_EXTENDED','Requested duration would not extend the current deadline.')
            response['warnings'].append('Renewal updates controller policy only; firmware does not acknowledge the server lease revision.')
        if action == 'uart.write':
            if len(params['data_hex']) > 512:
                reason('unsupported', 'UART_PAYLOAD_TOO_LARGE', 'Legacy writes are limited to 256 bytes.')
            if session and c.execute("SELECT 1 FROM debug_tx WHERE session_id=? AND state IN ('queued','dispatched')", (session['id'],)).fetchone():
                reason('blocked', 'UART_WRITE_UNCONFIRMED', 'Previous legacy write has not been confirmed.')
            if session and c.execute("SELECT 1 FROM workspace_operations WHERE session_id=? AND action='uart.write' AND outcome='unknown'", (session['id'],)).fetchone():
                reason('blocked', 'PREVIOUS_RESULT_UNKNOWN', 'Resolve the previous write or close this context before another write.')
    elif live:
        reason('blocked', 'DEBUG_CONTEXT_OCCUPIED', 'A legacy context still holds or may hold the node resources.')
    return finish()


def register(app, db, admin, bench):
    @app.post('/api/workspace/operation-plan', dependencies=[Depends(admin)])
    async def operation_plan(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
                raise HTTPException(413, {'code': 'REQUEST_TOO_LARGE', 'message': 'Plan request exceeds 256 KiB.'})
            raw.extend(chunk)
        document = parse_request(bytes(raw))
        owner = hashlib.sha256(request.cookies.get('fleet_session', '').encode()).hexdigest()
        with db() as c:
            c.execute('BEGIN')
            return assess(c, document, bench, owner)

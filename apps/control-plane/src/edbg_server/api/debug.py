"""Short-lived, boot-bound UART sessions for the explicitly wired two-board bench."""
import hashlib
import json
import secrets
import time

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from . import wave as wave_api
from . import i2c as i2c_api
from edbg_server import streams

BENCH = {'s3-fc012cd58460': 'target', 's3-fc012cca2cf8': 'probe'}
LIVE = ('opening', 'active', 'closing', 'expired')


def profiles():
    from edbg_server.paths import HARDWARE_ROOT
    descriptor = json.loads((HARDWARE_ROOT / 'boards/esp32s3-gpio-v1.json').read_text(encoding='utf-8'))
    descriptor['members'] = [{'device_id': did, 'role': role} for did, role in BENCH.items()]
    return [descriptor]


class Frame(BaseModel):
    seq: int = Field(ge=1, le=4294967295)
    port: int = Field(ge=1, le=2)
    us: int = Field(ge=0)
    data: str = Field(min_length=2, max_length=256, pattern=r'^(?:[0-9a-fA-F]{2})+$')


class Snapshot(BaseModel):
    session: str = Field(default='', max_length=24)
    state: str = Field(default='idle', max_length=16)
    error: str = Field(default='', max_length=96)
    tx_id: str = Field(default='', max_length=24)
    tx_error: str = Field(default='', max_length=96)
    dropped: int = Field(default=0, ge=0)
    uart_errors: int = Field(default=0, ge=0)
    rx: list[Frame] = Field(default_factory=list, max_length=16)
    wave: wave_api.Report | None = None
    i2c: i2c_api.Report | None = None


class OpenSession(BaseModel):
    device_id: str


class Send(BaseModel):
    data: str = Field(min_length=2, max_length=512, pattern=r'^(?:[0-9a-fA-F]{2})+$')
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')


def init(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS debug_sessions (
      id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
      role TEXT NOT NULL, boot_id TEXT NOT NULL, owner TEXT NOT NULL,
      state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
      updated REAL NOT NULL, snapshot TEXT NOT NULL DEFAULT '{}');
    CREATE UNIQUE INDEX IF NOT EXISTS one_debug_session ON debug_sessions(device_id)
      WHERE state IN ('opening','active','closing','expired');
    CREATE TABLE IF NOT EXISTS debug_tx (
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES debug_sessions(id),
      request_id TEXT NOT NULL, data TEXT NOT NULL, state TEXT NOT NULL,
      created REAL NOT NULL, expires REAL NOT NULL, detail TEXT NOT NULL DEFAULT '',
      UNIQUE(session_id, request_id));
    CREATE TABLE IF NOT EXISTS debug_rx (
      id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES debug_sessions(id),
      seq INTEGER NOT NULL, port INTEGER NOT NULL, us INTEGER NOT NULL,
      data TEXT NOT NULL, created REAL NOT NULL, UNIQUE(session_id, seq));
    ''')
    wave_api.init(c)
    i2c_api.init(c)


def expire(c, now):
    c.execute("UPDATE debug_sessions SET state='expired',updated=? WHERE state IN ('opening','active') AND expires<=?", (now, now))
    c.execute("UPDATE debug_tx SET state=CASE WHEN state='dispatched' THEN 'unknown' ELSE 'expired' END,detail='未在有效期内收到确认；不会再次发送' WHERE state IN ('queued','dispatched') AND (expires<=? OR session_id IN (SELECT id FROM debug_sessions WHERE state NOT IN ('opening','active')))", (now,))
    wave_api.expire(c, now)
    i2c_api.expire(c, now)


def check_idle(c, ids):
    for device_id in ids:
        if c.execute('SELECT 1 FROM workspace_update_operations WHERE node_id=? AND held=1',(device_id,)).fetchone():
            raise HTTPException(409, '节点维护尚未确认完成，请查询原操作')
        if c.execute("SELECT 1 FROM debug_sessions WHERE device_id=? AND state IN ('opening','active','closing','expired')", (device_id,)).fetchone():
            raise HTTPException(409, '请先结束调试会话，并等待设备确认释放串口')


def process(c, device_id, boot_id, snap, now):
    expire(c, now)
    row = c.execute("SELECT * FROM debug_sessions WHERE device_id=? AND state IN ('opening','active','closing','expired')", (device_id,)).fetchone()
    if not row:
        return None
    sid = row['id']
    if row['boot_id'] != boot_id:
        c.execute("UPDATE debug_sessions SET state='lost',updated=? WHERE id=?", (now, sid))
        c.execute("UPDATE debug_tx SET state='unknown',detail='设备已重启，未确认的发送不会重放' WHERE session_id=? AND state IN ('queued','dispatched')", (sid,))
        return None
    managed=c.execute('SELECT 1 FROM workspace_uart_sessions WHERE session_id=? UNION SELECT 1 FROM workspace_i2c_operations WHERE session_id=?',(sid,sid)).fetchone()
    if managed and row['state']=='active' and (snap is None or snap.session!=sid or snap.state not in ('active','expired','error')):
        state='closed' if snap and not snap.session and snap.state=='idle' else 'closing'
        previous=json.loads(row['snapshot'])
        previous['controller_observation']={'code':'ACTIVE_CONTEXT_NOT_OBSERVED','reported_session':snap.session if snap else None,
                                            'reported_state':snap.state if snap else None,'observed_at':now}
        c.execute('UPDATE debug_sessions SET state=?,updated=?,snapshot=? WHERE id=?',(state,now,json.dumps(previous),sid))
        c.execute("UPDATE debug_tx SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'unknown' END,detail='Managed context no longer observed; never reopen or replay automatically' WHERE session_id=? AND state IN ('queued','dispatched')",(sid,))
        return None
    if snap:
        if snap.session == sid:
            summary = snap.model_dump(exclude={'rx', 'wave', 'i2c'})
            previous = json.loads(row['snapshot'])
            for counter in ('dropped', 'uart_errors'):
                summary[counter] = max(summary.get(counter, 0), previous.get(counter, 0))
            if snap.wave:
                summary['wave'] = snap.wave.model_dump(exclude={'segments'})
            c.execute('UPDATE debug_sessions SET snapshot=?,updated=? WHERE id=?', (json.dumps(summary), now, sid))
            streams.ingest(c, row, snap, now)
            if snap.state in ('expired', 'error'):
                c.execute("UPDATE debug_sessions SET state='closed',updated=? WHERE id=?", (now, sid))
                expire(c, now)
                return None
            for frame in snap.rx:
                if frame.port == 2 and row['role'] != 'probe':
                    continue
                c.execute('INSERT OR IGNORE INTO debug_rx(session_id,seq,port,us,data,created) VALUES(?,?,?,?,?,?)', (sid, frame.seq, frame.port, frame.us, frame.data.lower(), now))
            c.execute('DELETE FROM debug_rx WHERE session_id=? AND id NOT IN (SELECT id FROM debug_rx WHERE session_id=? ORDER BY id DESC LIMIT 2000)', (sid, sid))
            if snap.tx_id:
                c.execute("UPDATE debug_tx SET state=?,detail=? WHERE id=? AND session_id=? AND state='dispatched'", ('failed' if snap.tx_error else 'sent', snap.tx_error or 'UART 发送完成', snap.tx_id, sid))
            if snap.state == 'active' and row['state'] == 'opening':
                c.execute("UPDATE debug_sessions SET state='active' WHERE id=?", (sid,))
        elif not snap.session and snap.state == 'idle' and row['state'] in ('closing', 'expired'):
            summary = snap.model_dump(exclude={'rx', 'wave', 'i2c'})
            previous = json.loads(row['snapshot'])
            for counter in ('dropped', 'uart_errors'):
                summary[counter] = max(summary.get(counter, 0), previous.get(counter, 0))
            c.execute("UPDATE debug_sessions SET state='closed',updated=?,snapshot=? WHERE id=?", (now, json.dumps(summary), sid))
            return None
    if row['state'] not in ('opening', 'active'):
        return None
    control = {'session': sid, 'role': row['role'], 'lease_ms': min(10000, max(0, int((row['expires'] - now) * 1000))), 'rx_ack': c.execute('SELECT COALESCE(MAX(seq),0) FROM debug_rx WHERE session_id=?', (sid,)).fetchone()[0]}
    control['rx_ack'] = streams.acknowledgement(c, sid, control['rx_ack'])
    tx = c.execute("""SELECT t.* FROM debug_tx t WHERE t.session_id=? AND t.state IN ('queued','dispatched') AND t.expires>?
        AND (t.state='queued' OR NOT EXISTS(SELECT 1 FROM workspace_operations o WHERE o.tx_id=t.id))
        ORDER BY t.created LIMIT 1""", (sid, now)).fetchone()
    if tx:
        c.execute("UPDATE debug_tx SET state='dispatched' WHERE id=?", (tx['id'],))
        control['tx'] = {'id': tx['id'], 'data': tx['data']}
    control['wave'] = wave_api.process(c, sid, snap.wave if snap and snap.session == sid else None, now)
    control['i2c'] = i2c_api.process(c, sid, snap.i2c if snap and snap.session == sid else None, now)
    return control


def install(app, db, lock, audit, admin):
    def owner(request):
        return hashlib.sha256(request.cookies.get('fleet_session', '').encode()).hexdigest()

    @app.get('/api/debug/sessions', dependencies=[Depends(admin)])
    def history(device_id: str, before: str = '', limit: int = Query(default=40, ge=1, le=100)):
        with db() as c:
            args = [device_id]
            where = 'device_id=?'
            if before:
                cursor = c.execute('SELECT created,id FROM debug_sessions WHERE id=? AND device_id=?', (before, device_id)).fetchone()
                if not cursor:
                    raise HTTPException(404, '历史会话游标不存在')
                where += ' AND (created,id)<(?,?)'
                args.extend([cursor['created'], cursor['id']])
            rows = [dict(r) for r in c.execute(
                f'SELECT id,device_id,boot_id,state,created,updated FROM debug_sessions WHERE {where} ORDER BY created DESC,id DESC LIMIT ?',
                (*args, limit + 1))]
        return {'sessions': rows[:limit], 'next': rows[limit - 1]['id'] if len(rows) > limit else None}

    @app.get('/api/debug/sessions/{sid}/record', dependencies=[Depends(admin)])
    def record(sid: str, after: int = Query(default=0, ge=0), limit: int = Query(default=256, ge=1, le=2000)):
        # One read snapshot keeps export contents and coverage metadata consistent.
        with db() as c:
            c.execute('BEGIN')
            session = c.execute('SELECT id,device_id,boot_id,state,created,updated,snapshot FROM debug_sessions WHERE id=?', (sid,)).fetchone()
            if not session:
                raise HTTPException(404, '调试会话不存在')
            session = dict(session)
            snapshot = json.loads(session.pop('snapshot'))
            retained = [dict(r) for r in c.execute('SELECT * FROM debug_rx WHERE session_id=? ORDER BY id', (sid,))]
            tx_count = c.execute('SELECT COUNT(*) FROM debug_tx WHERE session_id=?', (sid,)).fetchone()[0]
            transmissions = [dict(r) for r in c.execute('SELECT id,data,state,detail,created FROM debug_tx WHERE session_id=? ORDER BY created DESC,id DESC LIMIT 2000', (sid,))][::-1]
            available = [r for r in retained if r['id'] > after]
            first, last = (retained[0], retained[-1]) if retained else ({}, {})
            scope = {
                'retention_limit': 2000, 'retained_frames': len(retained),
                'retained_bytes': sum(len(r['data']) // 2 for r in retained),
                'first_id': first.get('id'), 'last_id': last.get('id'),
                'first_seq': first.get('seq'), 'last_seq': last.get('seq'),
                'first_received': first.get('created'), 'last_received': last.get('created'),
                'unavailable_before': max(0, first.get('seq', 1) - 1),
                'gaps_within': max(0, last.get('seq', 0) - first.get('seq', 1) + 1 - len(retained)),
                'device_reported_dropped': snapshot.get('dropped', 0),
                'tx_retained': len(transmissions), 'tx_omitted': tx_count - len(transmissions),
            }
        return {
            'session': session, 'scope': scope, 'frames': available[:limit],
            'transmissions': transmissions, 'has_more': len(available) > limit,
            'exported_at': time.time(),
            'time_basis': {'created': 'server time: RX report received; TX request submitted', 'us': 'RX device buffer read time since boot; not bit edge time'},
            'coverage_note': 'Only retained server records are included. Sequence gaps can result from retention or missing reports; unreported device data cannot be reconstructed.',
        }

    @app.get('/api/debug', dependencies=[Depends(admin)])
    def overview(request: Request):
        now = time.time()
        with lock, db() as c:
            expire(c, now)
            rows = [dict(r) for r in c.execute("SELECT * FROM debug_sessions ORDER BY (state IN ('opening','active','closing','expired')) DESC,created DESC LIMIT 40")]
            for row in rows:
                row['mine'] = row.pop('owner') == owner(request)
                row['snapshot'] = json.loads(row['snapshot'])
                row['tx'] = [dict(r) for r in c.execute('SELECT id,request_id,state,detail,created FROM debug_tx WHERE session_id=? ORDER BY created DESC LIMIT 5', (row['id'],))]
            waves = wave_api.rows(c, owner(request))
            i2c_runs = i2c_api.rows(c, owner(request))
        return {'bench': BENCH, 'profiles': profiles(), 'sessions': rows, 'server_time': now, 'waves': waves, 'i2c_runs': i2c_runs}

    @app.post('/api/debug/sessions', dependencies=[Depends(admin)])
    def open_session(body: OpenSession, request: Request):
        now = time.time()
        with lock, db() as c:
            c.execute('BEGIN IMMEDIATE')
            expire(c, now)
            row = c.execute('SELECT * FROM devices WHERE id=?', (body.device_id,)).fetchone()
            if body.device_id not in BENCH or not row or row['revoked']:
                raise HTTPException(400, '此设备尚未配置可用的调试接口')
            if now - row['last_seen'] > 8:
                raise HTTPException(409, '设备离线或心跳过旧，调试操作不离线排队')
            if 'debug_uart_v1' not in json.loads(row['telemetry']).get('capabilities', []):
                raise HTTPException(409, '请先安装支持远程串口的固件')
            check_idle(c, [body.device_id])
            if c.execute("SELECT 1 FROM jobs WHERE device_id=? AND state IN ('queued','dispatched','running','rebooting')", (body.device_id,)).fetchone():
                raise HTTPException(409, '请等待设备任务结束')
            sid = secrets.token_hex(12)
            c.execute('INSERT INTO debug_sessions(id,device_id,role,boot_id,owner,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)', (sid, body.device_id, BENCH[body.device_id], row['boot_id'], owner(request), 'opening', now, now + 300, now))
            audit(c, 'debug_open', f'{body.device_id} {sid}')
        return {'id': sid}

    @app.post('/api/debug/sessions/{sid}/send', dependencies=[Depends(admin)])
    def send(sid: str, body: Send, request: Request):
        now = time.time()
        with lock, db() as c:
            c.execute('BEGIN IMMEDIATE')
            expire(c, now)
            row = c.execute('SELECT * FROM debug_sessions WHERE id=?', (sid,)).fetchone()
            if not row or row['owner'] != owner(request):
                raise HTTPException(403, '该会话由其他浏览器持有')
            old = c.execute('SELECT * FROM debug_tx WHERE session_id=? AND request_id=?', (sid, body.request_id)).fetchone()
            if old:
                if old['data'] != body.data.lower():
                    raise HTTPException(409, '请求编号已用于不同数据')
                return {'id': old['id'], 'state': old['state']}
            dev = c.execute('SELECT * FROM devices WHERE id=?', (row['device_id'],)).fetchone()
            if row['state'] != 'active' or dev['revoked'] or now - dev['last_seen'] > 8 or dev['boot_id'] != row['boot_id']:
                raise HTTPException(409, '会话未就绪、已结束或设备离线，未发送数据')
            if c.execute("SELECT 1 FROM debug_tx WHERE session_id=? AND state IN ('queued','dispatched')", (sid,)).fetchone():
                raise HTTPException(409, '上一条发送仍在等待设备确认')
            tid = secrets.token_hex(12)
            c.execute('INSERT INTO debug_tx(id,session_id,request_id,data,state,created,expires) VALUES(?,?,?,?,?,?,?)', (tid, sid, body.request_id, body.data.lower(), 'queued', now, now + 10))
            audit(c, 'debug_send', f'{sid} {tid} bytes={len(body.data)//2}')
        return {'id': tid, 'state': 'queued'}

    @app.post('/api/debug/sessions/{sid}/close', dependencies=[Depends(admin)])
    def close(sid: str):
        with lock, db() as c:
            if c.execute('SELECT 1 FROM workspace_uart_sessions WHERE session_id=? UNION SELECT 1 FROM workspace_i2c_operations WHERE session_id=?', (sid,sid)).fetchone():
                raise HTTPException(409, 'Managed session: close through workspace operation API')
            c.execute("UPDATE debug_sessions SET state='closing',updated=? WHERE id=? AND state IN ('opening','active','expired')", (time.time(), sid))
            expire(c, time.time())
            audit(c, 'debug_close', sid)
        return {'ok': True}

    @app.get('/api/debug/sessions/{sid}/rx', dependencies=[Depends(admin)])
    def rx(sid: str, after: int = Query(default=0, ge=0)):
        with db() as c:
            return [dict(r) for r in c.execute('SELECT * FROM debug_rx WHERE session_id=? AND id>? ORDER BY id LIMIT 256', (sid, after))]

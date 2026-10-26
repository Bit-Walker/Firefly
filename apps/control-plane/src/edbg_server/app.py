"""ESP32 fleet control plane. SQLite is authoritative; devices initiate all traffic."""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from edbg_server.api import debug as debug_api
from edbg_server import migrations, workspace, planning, operations, streams, i2c_operations, artifacts, node_updates
from edbg_server.runtime import AgentRuntime

from edbg_server.paths import LEGACY_ROOT, WEB_ROOT

ROOT = LEGACY_ROOT
STATE = Path(os.environ.get('FLEET_STATE', ROOT / '.state')).resolve()
artifacts.STATE = STATE
STATE.mkdir(parents=True, exist_ok=True)
(STATE / 'firmware').mkdir(exist_ok=True)
DB = STATE / 'fleet.sqlite3'
LOCK = threading.RLock()
ACTIVE = ('queued', 'dispatched', 'running', 'rebooting')
TERMINAL = ('succeeded', 'failed', 'cancelled')


@contextmanager
def db():
    connection = sqlite3.connect(DB, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


with db() as c:
    c.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS devices (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, group_name TEXT NOT NULL,
      token_hash TEXT NOT NULL, revoked INTEGER DEFAULT 0, last_seen REAL DEFAULT 0,
      boot_id TEXT DEFAULT '', version TEXT DEFAULT '', partition TEXT DEFAULT '',
      telemetry TEXT DEFAULT '{}');
    CREATE TABLE IF NOT EXISTS firmware (
      id TEXT PRIMARY KEY, version TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
      size INTEGER NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
      kind TEXT NOT NULL, firmware_id TEXT REFERENCES firmware(id),
      state TEXT NOT NULL, progress INTEGER DEFAULT 0, detail TEXT DEFAULT '',
      source_boot TEXT DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
    CREATE UNIQUE INDEX IF NOT EXISTS one_active_job ON jobs(device_id)
      WHERE state IN ('queued','dispatched','running','rebooting');
    CREATE TABLE IF NOT EXISTS logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
      boot_id TEXT NOT NULL, seq INTEGER NOT NULL, line TEXT NOT NULL,
      created REAL NOT NULL, UNIQUE(device_id,boot_id,seq));
    CREATE TABLE IF NOT EXISTS audit (
      id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
      action TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS firmware_withdrawals (
      firmware_id TEXT PRIMARY KEY REFERENCES firmware(id), reason TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (
      token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
    ''')
    debug_api.init(c)

migrations.migrate(DB)
with db() as c:
    c.execute('BEGIN IMMEDIATE')
    workspace.import_legacy(c, debug_api.BENCH)
    streams.import_existing(c)

admin_path = STATE / 'admin.local.json'
if not admin_path.exists():
    admin_path.write_text(json.dumps({'username': 'admin', 'password': secrets.token_urlsafe(18)}), encoding='utf-8')
ADMIN = json.loads(admin_path.read_text(encoding='utf-8'))
app = FastAPI(title='Firefly', docs_url=None, redoc_url=None, openapi_url=None)
LOGIN_FAILURES = {}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def audit(c, action, detail):
    c.execute('INSERT INTO audit(created,action,detail) VALUES(?,?,?)', (time.time(), action, detail))


@app.middleware('http')
async def security(request: Request, call_next):
    # Browser mutations require a same-origin request. Device APIs use bearer credentials.
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not request.url.path.startswith('/api/device/'):
        origin = request.headers.get('origin')
        if origin != f'{request.url.scheme}://{request.headers.get("host")}':
            return Response('Origin rejected', status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    return response


def admin(request: Request):
    token = request.cookies.get('fleet_session', '')
    with db() as c:
        row = c.execute('SELECT expires FROM sessions WHERE token_hash=?', (digest(token),)).fetchone()
    if not row or row['expires'] < time.time():
        raise HTTPException(401, 'Please sign in')


def device(device_id: str, authorization: str = Header(default='')):
    with db() as c:
        row = c.execute('SELECT * FROM devices WHERE id=?', (device_id,)).fetchone()
    token = authorization.removeprefix('Bearer ')
    if not row or row['revoked'] or not authorization.startswith('Bearer ') or not secrets.compare_digest(row['token_hash'], digest(token)):
        raise HTTPException(401, 'Invalid device credentials')
    return dict(row)


class Login(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=128)


@app.post('/api/login')
def login(body: Login, request: Request, response: Response):
    peer = request.client.host if request.client else 'unknown'
    with LOCK:
        failures = [x for x in LOGIN_FAILURES.get(peer, []) if time.time() - x < 60]
        if len(failures) >= 5:
            raise HTTPException(429, '登录尝试过多，请稍后再试')
    # Slow password guessing without storing raw submitted credentials.
    if not (secrets.compare_digest(body.username, ADMIN['username']) and secrets.compare_digest(body.password, ADMIN['password'])):
        with LOCK:
            if len(LOGIN_FAILURES) > 1000:
                LOGIN_FAILURES.clear()
            LOGIN_FAILURES[peer] = failures + [time.time()]
        time.sleep(0.5)
        raise HTTPException(401, '用户名或密码错误')
    token = secrets.token_urlsafe(32)
    with LOCK:
        LOGIN_FAILURES.pop(peer, None)
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
        c.execute('INSERT INTO sessions VALUES(?,?)', (digest(token), time.time() + 8 * 3600))
        audit(c, 'login', request.client.host if request.client else 'unknown')
    response.set_cookie('fleet_session', token, httponly=True, secure=request.url.scheme == 'https', samesite='strict', max_age=8 * 3600)
    return {'ok': True}


@app.post('/api/logout', dependencies=[Depends(admin)])
def logout(request: Request, response: Response):
    with db() as c:
        c.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies.get('fleet_session', '')),))
    response.delete_cookie('fleet_session')
    return {'ok': True}


class Enrollment(BaseModel):
    id: str = Field(pattern=r'^s3-[0-9a-f]{12}$')
    name: str = Field(min_length=1, max_length=64)
    group: str = Field(default='默认', max_length=64)


@app.post('/api/devices', dependencies=[Depends(admin)])
def enroll(body: Enrollment):
    token = secrets.token_urlsafe(32)
    try:
        with LOCK, db() as c:
            c.execute('INSERT INTO devices(id,name,group_name,token_hash) VALUES(?,?,?,?)', (body.id, body.name, body.group, digest(token)))
            audit(c, 'enroll', body.id)
    except sqlite3.IntegrityError:
        raise HTTPException(409, '设备已登记')
    return {'id': body.id, 'token': token, 'note': '设备凭证仅显示一次，请保存并用于首次配置。'}


@app.get('/api/devices', dependencies=[Depends(admin)])
def list_devices():
    with db() as c:
        rows = c.execute('SELECT id,name,group_name,revoked,last_seen,boot_id,version,partition,telemetry FROM devices ORDER BY name').fetchall()
    return [{**dict(row), 'telemetry': json.loads(row['telemetry']), 'online': time.time() - row['last_seen'] < 20 and not row['revoked']} for row in rows]


@app.post('/api/devices/{device_id}/revoke', dependencies=[Depends(admin)])
def revoke(device_id: str):
    with LOCK, db() as c:
        c.execute('UPDATE devices SET revoked=1 WHERE id=?', (device_id,))
        c.execute("UPDATE jobs SET state='cancelled',detail='Device credentials revoked',updated=? WHERE device_id=? AND state IN ('queued','dispatched','running','rebooting') AND NOT EXISTS(SELECT 1 FROM workspace_update_operations u JOIN workspace_operations o ON o.id=u.operation_id WHERE u.job_id=jobs.id AND o.published=1)", (time.time(), device_id))
        audit(c, 'revoke', device_id)
    return {'ok': True, 'note': '已撤销后续访问；设备已经接收的操作无法保证立即停止。'}


def image_metadata(data):
    return artifacts.image_metadata(data)


@app.post('/api/firmware', dependencies=[Depends(admin)])
async def upload_firmware(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 0x300000:
            raise HTTPException(413, '固件超过 3 MB')
        data.extend(chunk)
    try:
        version = image_metadata(data)
    except UnicodeError:
        raise HTTPException(400, '固件描述无效')
    sha = hashlib.sha256(data).hexdigest()
    firmware_id = sha[:24]
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE')
        artifacts.store(c,data)
        audit(c, 'firmware_upload', f'{firmware_id} {version}')
    return {'id': firmware_id, 'version': version, 'sha256': sha, 'size': len(data)}


@app.get('/api/firmware', dependencies=[Depends(admin)])
def list_firmware():
    with db() as c:
        return [dict(row) for row in c.execute("SELECT f.*,COALESCE(w.reason,'') AS withdrawn FROM firmware f LEFT JOIN firmware_withdrawals w ON w.firmware_id=f.id ORDER BY f.created DESC")]


class Withdrawal(BaseModel):
    reason: str = Field(min_length=1, max_length=255)


@app.post('/api/firmware/{firmware_id}/withdraw', dependencies=[Depends(admin)])
def withdraw_firmware(firmware_id: str, body: Withdrawal):
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM firmware WHERE id=?', (firmware_id,)).fetchone():
            raise HTTPException(404, '固件不存在')
        c.execute('INSERT OR REPLACE INTO firmware_withdrawals VALUES(?,?,?)', (firmware_id, body.reason, time.time()))
        c.execute("UPDATE jobs SET state='cancelled',detail='目标固件已撤回',updated=? WHERE firmware_id=? AND state='queued'", (time.time(), firmware_id))
        audit(c, 'firmware_withdraw', f'{firmware_id} {body.reason}')
    return {'ok': True}


class Command(BaseModel):
    device_ids: list[str] = Field(min_length=1, max_length=100)
    kind: Literal['diagnostics', 'restart']
    firmware_id: None = None


@app.post('/api/jobs', dependencies=[Depends(admin)])
def create_jobs(body: Command):
    ids = list(dict.fromkeys(body.device_ids))
    now = time.time()
    result = []
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE')
        debug_api.check_idle(c, ids)
        for device_id in ids:
            row = c.execute('SELECT revoked FROM devices WHERE id=?', (device_id,)).fetchone()
            if not row or row['revoked']:
                raise HTTPException(400, f'设备不存在或已撤销：{device_id}')
            if c.execute("SELECT 1 FROM jobs WHERE device_id=? AND state IN ('queued','dispatched','running','rebooting')", (device_id,)).fetchone():
                raise HTTPException(409, f'设备还有未完成任务：{device_id}')
        for device_id in ids:
            job_id = secrets.token_hex(12)
            c.execute('INSERT INTO jobs(id,device_id,kind,firmware_id,state,created,updated) VALUES(?,?,?,?,?,?,?)', (job_id, device_id, body.kind, None, 'queued', now, now))
            result.append(job_id)
        audit(c, 'create_jobs', json.dumps({'kind': body.kind, 'devices': ids, 'firmware': body.firmware_id}))
    return {'job_ids': result}


@app.get('/api/jobs', dependencies=[Depends(admin)])
def list_jobs():
    with db() as c:
        return [dict(row) for row in c.execute('SELECT jobs.*,firmware.version AS target_version FROM jobs LEFT JOIN firmware ON jobs.firmware_id=firmware.id ORDER BY jobs.created DESC LIMIT 200')]


@app.get('/api/summary', dependencies=[Depends(admin)])
def summary():
    with db() as c:
        count = c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','dispatched','running','rebooting')").fetchone()[0]
    return {'active_jobs': count}


@app.post('/api/jobs/{job_id}/cancel', dependencies=[Depends(admin)])
def cancel(job_id: str):
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE')
        if node_updates.for_job(c,job_id):
            raise HTTPException(409,'请通过统一操作接口取消受管更新')
        row = c.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['state'] != 'queued':
            raise HTTPException(409, '只有尚未下发的任务可以取消')
        c.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?", (time.time(), job_id))
        audit(c, 'cancel', job_id)
    return {'ok': True}


@app.get('/api/devices/{device_id}/logs', dependencies=[Depends(admin)])
def logs(device_id: str, after: int = Query(default=0, ge=0), boot_id: str | None = None,
         tail: bool = False, limit: int = Query(default=300, ge=1, le=1000)):
    with db() as c:
        where = 'device_id=? AND id>?'
        parameters = [device_id, after]
        if boot_id is not None:
            where += ' AND boot_id=?'
            parameters.append(boot_id)
        order = 'DESC' if tail else 'ASC'
        rows = [dict(row) for row in c.execute(f'SELECT * FROM logs WHERE {where} ORDER BY id {order} LIMIT ?', (*parameters, limit))]
        return list(reversed(rows)) if tail else rows


class Log(BaseModel):
    seq: int = Field(ge=0)
    line: str = Field(max_length=255)


class Report(BaseModel):
    id: str = Field(max_length=24)
    state: Literal['running', 'rebooting', 'succeeded', 'failed']
    progress: int = Field(default=0, ge=0, le=100)
    detail: str = Field(default='', max_length=512)


class Poll(BaseModel):
    boot_id: str = Field(min_length=1, max_length=32)
    version: str = Field(max_length=31)
    partition: str = Field(max_length=16)
    uptime: int = Field(ge=0)
    heap: int = Field(ge=0)
    min_heap: int = Field(ge=0)
    rssi: int = Field(ge=-127, le=0)
    reset_reason: int = 0
    logs: list[Log] = Field(default_factory=list, max_length=16)
    report: Report | None = None
    capabilities: list[str] = Field(default_factory=list, max_length=8)
    debug: debug_api.Snapshot | None = None
    runtime: AgentRuntime | None = None

    @model_validator(mode='after')
    def consistent_runtime(self):
        if self.runtime and (self.runtime.build!=self.version or len(set(self.runtime.features))!=len(self.runtime.features)):
            raise ValueError('Runtime build and unique features must agree with the poll')
        return self


@app.post('/api/device/{device_id}/poll')
def poll(body: Poll, identity=Depends(device)):
    device_id = identity['id']
    now = time.time()
    ack = None
    command = None
    with LOCK, db() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('UPDATE devices SET last_seen=?,boot_id=?,version=?,partition=?,telemetry=? WHERE id=?', (now, body.boot_id, body.version, body.partition, json.dumps(body.model_dump(exclude={'logs', 'report', 'debug'})), device_id))
        for item in body.logs:
            c.execute('INSERT OR IGNORE INTO logs(device_id,boot_id,seq,line,created) VALUES(?,?,?,?,?)', (device_id, body.boot_id, item.seq, item.line, now))
        c.execute('DELETE FROM logs WHERE device_id=? AND id NOT IN (SELECT id FROM logs WHERE device_id=? ORDER BY id DESC LIMIT 10000)', (device_id, device_id))
        operations.refresh(c,now,device_id)
        if body.report:
            report = body.report
            managed=node_updates.for_job(c,report.id,device_id)
            job = c.execute('SELECT jobs.*,firmware.version AS target_version FROM jobs LEFT JOIN firmware ON jobs.firmware_id=firmware.id WHERE jobs.id=? AND device_id=?', (report.id, device_id)).fetchone()
            if managed:
                ack=node_updates.process_report(c,managed,body,now)
            elif job and job['state'] in TERMINAL:
                ack = report.id
            elif job and job['state'] != 'queued':
                state = report.state
                if job['kind']=='ota':
                    raise HTTPException(409,'OTA requires a managed node.update operation')
                if state == 'succeeded' and job['kind'] == 'restart' and body.boot_id == job['source_boot']:
                    raise HTTPException(409, 'Restart success requires a new boot')
                c.execute('UPDATE jobs SET state=?,progress=?,detail=?,updated=? WHERE id=?', (state, report.progress, report.detail, now, report.id))
                if state in TERMINAL:
                    ack = report.id
        job = c.execute("SELECT jobs.*,firmware.version AS target_version,firmware.sha256,firmware.size FROM jobs LEFT JOIN firmware ON jobs.firmware_id=firmware.id WHERE device_id=? AND jobs.state IN ('queued','dispatched','running','rebooting') ORDER BY jobs.created LIMIT 1", (device_id,)).fetchone()
        managed=node_updates.for_job(c,job['id'],device_id) if job else None
        if managed:
            command=node_updates.publish(c,managed,now)
        elif job and job['kind']!='ota' and (job['state'] == 'queued' or (job['state'] in ('dispatched', 'running') and now - job['updated'] > 30)):
            if job['state'] == 'queued':
                c.execute("UPDATE jobs SET state='dispatched',source_boot=?,updated=? WHERE id=?", (body.boot_id, now, job['id']))
            else:
                c.execute('UPDATE jobs SET updated=? WHERE id=?', (now, job['id']))
            command = {key: job[key] for key in ('id', 'kind', 'firmware_id', 'target_version', 'sha256', 'size')}
        operations.before_poll(c, device_id, now)
        i2c_operations.before_poll(c,device_id,body.boot_id,body.debug,now)
        debug_control = debug_api.process(c, device_id, body.boot_id, body.debug, now)
        debug_control=i2c_operations.after_poll(c,device_id,body.boot_id,body.debug,debug_control,now)
        operations.after_poll(c, device_id, body.boot_id, body.debug, debug_control, now)
        streams.synchronize(c, device_id, now)
    return {'server_time': int(now), 'report_ack': ack, 'command': command, 'debug_control': debug_control}


@app.get('/api/device/{device_id}/firmware/{firmware_id}')
def download(firmware_id: str, identity=Depends(device)):
    with db() as c:
        job = c.execute("""SELECT 1 FROM jobs j JOIN workspace_update_operations u ON u.job_id=j.id
            JOIN workspace_operations o ON o.id=u.operation_id WHERE j.device_id=? AND j.firmware_id=?
            AND j.state IN ('dispatched','running','rebooting') AND u.held=1 AND o.published=1""", (identity['id'], firmware_id)).fetchone()
        row = c.execute('SELECT * FROM firmware WHERE id=?', (firmware_id,)).fetchone()
        withdrawn = c.execute('SELECT 1 FROM firmware_withdrawals WHERE firmware_id=?', (firmware_id,)).fetchone()
    if not job or not row or withdrawn:
        raise HTTPException(403, 'No authorized OTA task for this image')
    if not artifacts.check_file(row):
        raise HTTPException(409,'Stored firmware integrity check failed')
    return FileResponse(STATE / 'firmware' / f'{firmware_id}.bin', media_type='application/octet-stream')


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/')
def index():
    return FileResponse(WEB_ROOT / 'index.html')


@app.get('/app.js')
def script():
    return FileResponse(WEB_ROOT / 'app.js', media_type='application/javascript')


@app.get('/style.css')
def style():
    return FileResponse(WEB_ROOT / 'style.css', media_type='text/css')


@app.get('/ui-model.js')
def ui_model():
    return FileResponse(WEB_ROOT / 'ui-model.js', media_type='application/javascript')


debug_api.install(app, db, LOCK, audit, admin)
debug_api.wave_api.install(app, db, LOCK, audit, admin)
debug_api.i2c_api.install(app, db, LOCK, audit, admin)


@app.get('/debug.js')
def debug_script():
    return FileResponse(WEB_ROOT / 'debug.js', media_type='application/javascript')


@app.get('/wave-model.js')
def wave_model_script():
    return FileResponse(WEB_ROOT / 'wave-model.js', media_type='application/javascript')


@app.get('/wave-view.js')
def wave_view_script():
    return FileResponse(WEB_ROOT / 'wave-view.js', media_type='application/javascript')


@app.get('/debug-model.js')
def debug_model_script():
    return FileResponse(WEB_ROOT / 'debug-model.js', media_type='application/javascript')


@app.get('/i2c-model.js')
def i2c_model_script():
    return FileResponse(WEB_ROOT / 'i2c-model.js', media_type='application/javascript')


@app.get('/i2c-view.js')
def i2c_view_script():
    return FileResponse(WEB_ROOT / 'i2c-view.js', media_type='application/javascript')


@app.get('/connection-model.js')
def connection_model_script():
    return FileResponse(WEB_ROOT / 'connection-model.js', media_type='application/javascript')


@app.get('/connections.js')
def connections_script():
    return FileResponse(WEB_ROOT / 'connections.js', media_type='application/javascript')


workspace.register(app, db, admin, audit)
planning.register(app, db, admin, debug_api.BENCH)
operations.register(app, db, admin, debug_api.BENCH, audit)
artifacts.register(app, db, admin, audit)
streams.register(app, db, admin)

"""Finite I2C master transactions, optionally verified by a paired responder."""
import hashlib
import json
import secrets
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

ACTIVE = ('arming', 'running', 'verifying')
BUS_KEY = 'esp32s3-gpio-v1:i2c0'


class Report(BaseModel):
    id: str = Field(default='', max_length=24)
    state: Literal['idle', 'ready', 'done', 'failed', 'cancelled'] = 'idle'
    error: str = Field(default='', max_length=96)
    data: str = Field(default='', max_length=128, pattern=r'^(?:[0-9a-fA-F]{2})*$')
    found: list[int] = Field(default_factory=list, max_length=112)
    elapsed_us: int = Field(default=0, ge=0)
    sda_idle: int | None = Field(default=None, ge=0, le=1)
    scl_idle: int | None = Field(default=None, ge=0, le=1)


class Run(BaseModel):
    model_config = ConfigDict(extra='forbid')
    master_session: str = Field(pattern=r'^[0-9a-f]{24}$')
    target_session: str | None = Field(default=None, pattern=r'^[0-9a-f]{24}$')
    mode: Literal['paired', 'external'] = 'paired'
    operation: Literal['scan', 'read', 'write', 'write_read'] = 'scan'
    address: int = Field(default=0x42, ge=0x08, le=0x77)
    data: str = Field(default='', max_length=128, pattern=r'^(?:[0-9a-fA-F]{2})*$')
    read_len: int = Field(default=0, ge=0, le=64)
    reply: str = Field(default='', max_length=128, pattern=r'^(?:[0-9a-fA-F]{2})*$')
    scan_start: int = Field(default=0x08, ge=0x08, le=0x77)
    scan_end: int = Field(default=0x77, ge=0x08, le=0x77)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')

    @model_validator(mode='after')
    def valid(self):
        if self.mode == 'paired' and not self.target_session:
            raise ValueError('配对应答器模式需要应答器会话')
        if self.mode == 'external' and (self.target_session or self.reply):
            raise ValueError('外设模式不创建应答器，不设置模拟应答')
        if self.master_session == self.target_session:
            raise ValueError('需要两个不同的调试会话')
        writes = self.operation in ('write', 'write_read')
        reads = self.operation in ('read', 'write_read')
        if bool(self.data) != writes or bool(self.read_len) != reads:
            raise ValueError('操作类型与读写长度不匹配')
        if self.mode == 'paired' and len(self.reply) != self.read_len * 2:
            raise ValueError('应答器数据字节数必须等于读取长度')
        if self.scan_start > self.scan_end:
            raise ValueError('扫描起始地址不能大于结束地址')
        self.data, self.reply = self.data.lower(), self.reply.lower()
        return self


def init(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS i2c_runs (
      id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
      master_session TEXT NOT NULL REFERENCES debug_sessions(id),
      target_session TEXT REFERENCES debug_sessions(id),params TEXT NOT NULL,
      state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,
      dispatched INTEGER NOT NULL DEFAULT 0,target_ready INTEGER NOT NULL DEFAULT 0,
      master_report TEXT NOT NULL DEFAULT '{}',target_report TEXT NOT NULL DEFAULT '{}',
      detail TEXT NOT NULL DEFAULT '',UNIQUE(owner,request_id));
    ''')
    # Existing databases required a responder. Preserve every historical row
    # while allowing NULL for independently executed master transactions.
    c.execute('BEGIN IMMEDIATE')
    try:
        if next(r for r in c.execute('PRAGMA table_info(i2c_runs)') if r['name']=='target_session')['notnull']:
            c.execute('''CREATE TABLE i2c_runs_new (
              id TEXT PRIMARY KEY,owner TEXT NOT NULL,request_id TEXT NOT NULL,
              master_session TEXT NOT NULL REFERENCES debug_sessions(id),
              target_session TEXT REFERENCES debug_sessions(id),params TEXT NOT NULL,
              state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,
              dispatched INTEGER NOT NULL DEFAULT 0,target_ready INTEGER NOT NULL DEFAULT 0,
              master_report TEXT NOT NULL DEFAULT '{}',target_report TEXT NOT NULL DEFAULT '{}',
              detail TEXT NOT NULL DEFAULT '',UNIQUE(owner,request_id))''')
            c.execute('INSERT INTO i2c_runs_new SELECT * FROM i2c_runs')
            c.execute('DROP TABLE i2c_runs')
            c.execute('ALTER TABLE i2c_runs_new RENAME TO i2c_runs')
        c.commit()
    except Exception:
        c.rollback()
        raise


def normalize(params):
    return {'mode': 'paired', 'scan_start': 8, 'scan_end': 0x77, 'bus_key': BUS_KEY, **params}


def expire(c, now):
    for r in c.execute("SELECT * FROM i2c_runs WHERE state IN ('arming','running','verifying')").fetchall():
        bad = False
        for sid in (r['master_session'], r['target_session']):
            if sid is None:
                continue
            s = c.execute('SELECT s.*,d.boot_id AS current_boot,d.last_seen,d.revoked FROM debug_sessions s JOIN devices d ON d.id=s.device_id WHERE s.id=?', (sid,)).fetchone()
            if not s or s['state'] != 'active' or s['expires'] <= now or s['boot_id'] != s['current_boot'] or s['revoked'] or now-s['last_seen'] > 8:
                bad = True
        if bad or r['expires'] <= now:
            # An unconfirmed write may have reached the bus. Never call it safe to replay.
            c.execute('UPDATE i2c_runs SET state=?,detail=?,updated=? WHERE id=?',
                      ('unknown' if r['dispatched'] else 'cancelled', '会话或操作已到期／设备状态变化；已下发事务可能执行，禁止自动重试' if r['dispatched'] else '准备阶段结束，未下发总线事务', now, r['id']))


def process(c, sid, report, now):
    expire(c, now)
    r = c.execute("SELECT * FROM i2c_runs WHERE state IN ('arming','running','verifying') AND (master_session=? OR target_session=?)", (sid, sid)).fetchone()
    if not r:
        return None
    master = sid == r['master_session']
    if report and report.id == r['id']:
        if report.state in ('failed', 'cancelled'):
            detail = ('地址或数据未获应答（NACK），请核对目标地址和接线' if 'NACK' in report.error else
                      'I²C 等待超时，请检查线路电平和上拉' if report.error == 'ESP_ERR_TIMEOUT' else
                      'SDA 或 SCL 持续为低，未开始事务' if 'Bus not idle' in report.error else
                      '设备操作租约已到期，结果不完整' if 'lease expired' in report.error else report.error or '设备已停止事务')
            state = 'unknown' if report.state == 'cancelled' and r['dispatched'] else 'failed'
            c.execute('UPDATE i2c_runs SET state=?,detail=?,updated=? WHERE id=?', (state, detail, now, r['id']))
            field = 'master_report' if master else 'target_report'
            c.execute(f'UPDATE i2c_runs SET {field}=? WHERE id=?', (report.model_dump_json(), r['id']))
            return None
        if master and report.state == 'done':
            if not r['dispatched']:
                return None
            c.execute("UPDATE i2c_runs SET master_report=?,state='verifying',updated=? WHERE id=?", (report.model_dump_json(), now, r['id']))
        elif not master and report.state == 'ready':
            c.execute('UPDATE i2c_runs SET target_ready=1,target_report=?,updated=? WHERE id=?', (report.model_dump_json(), now, r['id']))
    r = c.execute('SELECT * FROM i2c_runs WHERE id=?', (r['id'],)).fetchone()
    p, m, t = (json.loads(r[k]) for k in ('params', 'master_report', 'target_report'))
    p = normalize(p)
    if m.get('state') == 'done':
        reading = p['operation'] in ('read', 'write_read')
        writing = p['operation'] in ('write', 'write_read')
        if p['mode'] == 'external':
            valid = len(m.get('data', '')) == p['read_len'] * 2
            found = m.get('found', [])
            if p['operation'] == 'scan':
                valid = valid and len(found)==len(set(found)) and all(p['scan_start']<=a<=p['scan_end'] for a in found)
            detail = (('扫描完成；发现 %d 个应答地址' % len(found)) if p['operation']=='scan' else '主机事务完成；已收到总线应答') if valid else '设备报告的数据长度或扫描地址范围与请求不一致'
            c.execute('UPDATE i2c_runs SET state=?,detail=?,updated=? WHERE id=?', ('completed' if valid else 'failed', detail, now, r['id']))
            return None
        # Wait for the independently reported target RX, not just the master's ACK.
        got = t.get('data', '').lower()
        if not writing or len(got) >= len(p['data']):
            valid = (not reading or m.get('data', '').lower() == p['reply']) and (not writing or got == p['data'])
            if p['operation']=='scan':
                found=m.get('found',[])
                valid=valid and len(found)==len(set(found)) and all(p['scan_start']<=a<=p['scan_end'] for a in found)
            c.execute('UPDATE i2c_runs SET state=?,detail=?,updated=? WHERE id=?',
                      ('completed' if valid else 'failed', ('扫描完成；发现 %d 个应答地址' % len(m.get('found', []))) if p['operation'] == 'scan' else '两端数据核对一致' if valid else '实际数据与事务配置不一致，请查看两端记录', now, r['id']))
            return None
    if not master:
        return {'id': r['id'], 'kind': 'target', **p}
    managed='workspace_operation_id' in r.keys() and r['workspace_operation_id'] is not None
    if (r['target_ready'] or p['mode']=='external') and not m.get('state') and (not managed or not r['dispatched']):
        c.execute("UPDATE i2c_runs SET dispatched=1,state='running',updated=? WHERE id=?", (now, r['id']))
        return {'id': r['id'], 'kind': 'master', **p}
    return None


def public(row):
    item = dict(row)
    item.pop('owner', None)
    for k in ('params', 'master_report', 'target_report'):
        item[k] = json.loads(item[k])
    item['params'] = normalize(item['params'])
    return item


def rows(c, owner):
    result=[]
    for row in c.execute('SELECT * FROM i2c_runs ORDER BY created DESC LIMIT 50'):
        item={**public(row), 'mine': row['owner']==owner}
        for role in ('master','target'):
            item[role]=dict(c.execute('SELECT device_id,boot_id FROM debug_sessions WHERE id=?',(row[role+'_session'],)).fetchone()) if row[role+'_session'] else None
        result.append(item)
    return result


def install(app, db, lock, audit, admin):
    def owner(req):
        return hashlib.sha256(req.cookies.get('fleet_session', '').encode()).hexdigest()

    @app.post('/api/i2c', dependencies=[Depends(admin)])
    def create(body: Run, request: Request):
        now, who = time.time(), owner(request)
        params = body.model_dump(exclude={'request_id', 'master_session', 'target_session'})
        params.update(speed_hz=10000, sda=9, scl=10, target_address=0x42, internal_pullup=True)
        params['bus_key'] = BUS_KEY
        with lock, db() as c:
            c.execute('BEGIN IMMEDIATE')
            expire(c, now)
            if c.execute('SELECT 1 FROM workspace_i2c_operations WHERE held=1 LIMIT 1').fetchone():
                raise HTTPException(409,'Managed I2C operation still holds this adapter; wait for confirmed context release')
            old = c.execute('SELECT * FROM i2c_runs WHERE owner=? AND request_id=?', (who, body.request_id)).fetchone()
            if old:
                if old['master_session'] != body.master_session or old['target_session'] != body.target_session or normalize(json.loads(old['params'])) != params:
                    raise HTTPException(409, '请求编号已用于不同参数')
                return {'id': old['id'], 'state': old['state']}
            for sid, role in ((body.master_session, 'probe'), (body.target_session, 'target')):
                if sid is None:
                    continue
                s = c.execute('SELECT s.*,d.last_seen,d.boot_id AS current_boot,d.revoked,d.telemetry FROM debug_sessions s JOIN devices d ON d.id=s.device_id WHERE s.id=?', (sid,)).fetchone()
                if not s or s['owner'] != who:
                    raise HTTPException(403, '需要本浏览器持有的关联设备会话')
                if (body.mode=='paired' and s['role'] != role) or s['state'] != 'active' or s['expires']-now < 35 or now-s['last_seen'] > 8 or s['revoked'] or s['boot_id'] != s['current_boot']:
                    raise HTTPException(409, '关联会话未就绪、接口角色不符或剩余时间不足 35 秒')
                if 'i2c_bench_v1' not in json.loads(s['telemetry']).get('capabilities', []):
                    raise HTTPException(409, '关联设备需安装支持此 I²C 接口的固件')
                if sid==body.master_session and (body.mode=='external' or body.scan_start!=8 or body.scan_end!=0x77) and 'i2c_master_v2' not in json.loads(s['telemetry']).get('capabilities', []):
                    raise HTTPException(409, '独立主机及范围扫描需要更新设备固件')
                if c.execute("SELECT 1 FROM i2c_runs WHERE state IN ('arming','running','verifying') AND (master_session=? OR target_session=?)", (sid, sid)).fetchone():
                    raise HTTPException(409, '上一条 I²C 事务尚未结束')
                if c.execute("SELECT 1 FROM wave_runs WHERE state IN ('arming','capturing') AND (receiver_session=? OR source_session=?)", (sid, sid)).fetchone():
                    raise HTTPException(409, '请先结束波形采集')
            # Both configured boards share the same physical SDA/SCL wires.
            # Separate browser sessions must not drive that bus concurrently.
            if any(normalize(json.loads(r['params']))['bus_key']==BUS_KEY for r in c.execute("SELECT params FROM i2c_runs WHERE state IN ('arming','running','verifying')")):
                raise HTTPException(409, '此物理 I²C 总线正在被其他事务使用')
            rid = secrets.token_hex(12)
            c.execute('INSERT INTO i2c_runs(id,owner,request_id,master_session,target_session,params,state,created,updated,expires) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (rid, who, body.request_id, body.master_session, body.target_session, json.dumps(params), 'arming', now, now, now+30))
            audit(c, 'i2c_create', f'{rid} {body.operation} address={body.address} write={len(body.data)//2} read={body.read_len}')
        return {'id': rid, 'state': 'arming'}

    @app.get('/api/i2c/{rid}', dependencies=[Depends(admin)])
    def read(rid: str):
        with lock, db() as c:
            expire(c, time.time())
            row = c.execute('SELECT * FROM i2c_runs WHERE id=?', (rid,)).fetchone()
            if not row:
                raise HTTPException(404, 'I²C 记录不存在')
            result = public(row)
            for role in ('master', 'target'):
                result[role] = dict(c.execute('SELECT device_id,boot_id FROM debug_sessions WHERE id=?', (row[role+'_session'],)).fetchone()) if row[role+'_session'] else None
        return result

    @app.post('/api/i2c/{rid}/cancel', dependencies=[Depends(admin)])
    def cancel(rid: str):
        with lock, db() as c:
            row=c.execute('SELECT workspace_operation_id FROM i2c_runs WHERE id=?',(rid,)).fetchone()
            if row and row['workspace_operation_id']:
                raise HTTPException(409,'Managed I2C operation: cancel through workspace operation API')
            c.execute("UPDATE i2c_runs SET state=CASE WHEN dispatched=1 THEN 'unknown' ELSE 'cancelled' END,detail='停止后续下发；已开始的短事务可能完成，不会自动重试',updated=? WHERE id=? AND state IN ('arming','running','verifying')", (time.time(), rid))
            audit(c, 'i2c_cancel', rid)
        return {'ok': True}

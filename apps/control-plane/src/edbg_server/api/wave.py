"""Finite hardware pulse/capture operations, bound to existing debug leases."""
import hashlib
import json
import secrets
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from edbg_analysis.waveform import analyze

ACTIVE = ('arming', 'capturing')


class Segment(BaseModel):
    level: Literal[0, 1]
    us: int = Field(ge=1, le=32767)


class Report(BaseModel):
    id: str = Field(default='', max_length=24)
    state: Literal['idle', 'armed', 'sending', 'sent', 'captured', 'failed', 'cancelled'] = 'idle'
    gpio: int = Field(default=0, ge=0, le=48)
    error: str = Field(default='', max_length=96)
    started_us: int = Field(default=0, ge=0)
    finished_us: int = Field(default=0, ge=0)
    truncated: bool = False
    segments: list[Segment] = Field(default_factory=list, max_length=512)


class Run(BaseModel):
    model_config = ConfigDict(extra='forbid')
    receiver_session: str = Field(pattern=r'^[0-9a-f]{24}$')
    source_session: str | None = Field(default=None, pattern=r'^[0-9a-f]{24}$')
    mode: Literal['paired', 'capture'] = 'paired'
    high_us: int = Field(default=500, ge=50, le=10000)
    low_us: int = Field(default=500, ge=50, le=10000)
    count: int = Field(default=16, ge=1, le=64)
    request_id: str = Field(pattern=r'^[a-zA-Z0-9-]{16,64}$')

    @model_validator(mode='after')
    def bounded(self):
        if (self.high_us + self.low_us) * self.count > 1000000:
            raise ValueError('单次输出总时长不能超过 1 秒')
        if self.mode == 'paired' and (not self.source_session or self.source_session == self.receiver_session):
            raise ValueError('信号发生与采集需要两个不同会话')
        if self.mode == 'capture' and self.source_session:
            raise ValueError('仅采集不允许指定输出会话')
        return self


def init(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS wave_runs (
      id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
      receiver_session TEXT NOT NULL REFERENCES debug_sessions(id),
      source_session TEXT REFERENCES debug_sessions(id), mode TEXT NOT NULL,
      params TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
      expires REAL NOT NULL, updated REAL NOT NULL, rx_state TEXT DEFAULT '',
      tx_state TEXT DEFAULT '', detail TEXT DEFAULT '', capture TEXT DEFAULT '{}',
      stats TEXT DEFAULT '{}', UNIQUE(owner,request_id));
    ''')


def expire(c, now):
    for row in c.execute("SELECT * FROM wave_runs WHERE state IN ('arming','capturing')").fetchall():
        bad = None
        for sid in (row['receiver_session'], row['source_session']):
            if sid is None:
                continue
            s = c.execute('SELECT s.state,s.expires,s.boot_id,d.boot_id AS current_boot,d.revoked,d.last_seen FROM debug_sessions s JOIN devices d ON s.device_id=d.id WHERE s.id=?', (sid,)).fetchone()
            if not s or s['state'] != 'active' or s['expires'] <= now or s['boot_id'] != s['current_boot'] or s['revoked'] or now - s['last_seen'] > 8:
                bad = '调试会话已结束、设备离线或启动批次变化；操作已停止'
        if now >= row['expires'] or bad:
            c.execute("UPDATE wave_runs SET state=?,detail=?,updated=? WHERE id=?", ('cancelled' if bad else 'expired', bad or '波形操作超时；不会再次输出', now, row['id']))


def process(c, sid, report, now):
    expire(c, now)
    row = c.execute("SELECT * FROM wave_runs WHERE state IN ('arming','capturing') AND (receiver_session=? OR source_session=?) ORDER BY created LIMIT 1", (sid, sid)).fetchone()
    if not row:
        return None
    wid = row['id']
    receiver = sid == row['receiver_session']
    if report and report.id == wid:
        role = c.execute('SELECT role FROM debug_sessions WHERE id=?', (sid,)).fetchone()['role']
        expected = (6 if role == 'probe' else 7) if receiver else (7 if role == 'probe' else 6)
        if report.gpio != expected or report.state in ('failed', 'cancelled'):
            c.execute("UPDATE wave_runs SET state='failed',detail=?,updated=? WHERE id=?", (report.error or '设备取消操作或引脚不匹配', now, wid))
            return None
        if receiver:
            c.execute('UPDATE wave_runs SET rx_state=?,updated=? WHERE id=?', (report.state, now, wid))
            if report.state == 'armed':
                c.execute("UPDATE wave_runs SET state='capturing' WHERE id=?", (wid,))
            if report.state == 'captured':
                raw = report.model_dump()
                stats = analyze(raw['segments'])
                c.execute('UPDATE wave_runs SET capture=?,stats=? WHERE id=?', (json.dumps(raw), json.dumps(stats), wid))
                if report.truncated or not stats['high_pulses']:
                    c.execute("UPDATE wave_runs SET state='failed',detail='采集缓冲已满或没有完整高脉冲',updated=? WHERE id=?", (now, wid))
                    return None
                if row['mode'] == 'paired' and not row['tx_state']:
                    c.execute("UPDATE wave_runs SET state='failed',detail='输出前已经出现触发信号，请检查线路后重试',updated=? WHERE id=?", (now, wid))
                    return None
        else:
            c.execute('UPDATE wave_runs SET tx_state=?,updated=? WHERE id=?', (report.state, now, wid))
    row = c.execute('SELECT * FROM wave_runs WHERE id=?', (wid,)).fetchone()
    if row['rx_state'] == 'captured' and (row['mode'] == 'capture' or row['tx_state'] == 'sent'):
        stats, params = json.loads(row['stats']), json.loads(row['params'])
        valid = row['mode'] == 'capture' or stats.get('high_pulses') == params['count']
        reason = '实际脉冲数与输出设置不同，请查看原始采集'
        if valid and row['mode'] == 'paired':
            for metric, expected in [('high_us_mean', params['high_us']), ('low_us_mean', params['low_us'])]:
                value = stats.get(metric)
                if value is not None and abs(value-expected) > max(2,expected*0.03):
                    valid, reason = False, '实测脉宽与输出设置不同，请检查线路和原始采集'
        c.execute('UPDATE wave_runs SET state=?,detail=?,updated=? WHERE id=?', ('completed' if valid else 'failed', '采集完成；末尾空闲段未记录' if valid else reason, now, wid))
        return None
    if receiver:
        return {'id': wid, 'kind': 'capture'}
    if row['rx_state'] in ('armed', 'captured'):
        if not row['tx_state']:
            c.execute("UPDATE wave_runs SET tx_state='dispatched',updated=? WHERE id=?", (now, wid))
        return {'id': wid, 'kind': 'pulse', **json.loads(row['params'])}
    return None


def rows(c, owner):
    result = []
    for row in c.execute('SELECT * FROM wave_runs ORDER BY created DESC LIMIT 20'):
        item = dict(row)
        item['mine'] = item.pop('owner') == owner
        item['has_capture'] = item.pop('capture') != '{}'
        item['stats'] = json.loads(item['stats'])
        item['params'] = json.loads(item['params'])
        item['receiver_role'] = c.execute('SELECT role FROM debug_sessions WHERE id=?', (row['receiver_session'],)).fetchone()['role']
        result.append(item)
    return result


def install(app, db, lock, audit, admin):
    @app.post('/api/waves', dependencies=[Depends(admin)])
    def create(body: Run, request: Request):
        now = time.time()
        owner = hashlib.sha256(request.cookies.get('fleet_session', '').encode()).hexdigest()
        params = {'high_us': body.high_us, 'low_us': body.low_us, 'count': body.count}
        with lock, db() as c:
            c.execute('BEGIN IMMEDIATE')
            expire(c, now)
            old = c.execute('SELECT * FROM wave_runs WHERE owner=? AND request_id=?', (owner, body.request_id)).fetchone()
            if old:
                if old['receiver_session'] != body.receiver_session or old['source_session'] != body.source_session or old['mode'] != body.mode or json.loads(old['params']) != params:
                    raise HTTPException(409, '请求编号已用于不同波形参数')
                return {'id': old['id'], 'state': old['state']}
            sessions = []
            for sid in (body.receiver_session, body.source_session):
                if sid is None:
                    continue
                s = c.execute('SELECT s.*,d.last_seen,d.boot_id AS current_boot,d.revoked,d.telemetry FROM debug_sessions s JOIN devices d ON d.id=s.device_id WHERE s.id=?', (sid,)).fetchone()
                if not s or s['owner'] != owner:
                    raise HTTPException(403, '需要本浏览器持有的调试会话')
                if s['state'] != 'active' or s['expires'] - now < 40 or now - s['last_seen'] > 8 or s['revoked'] or s['boot_id'] != s['current_boot']:
                    raise HTTPException(409, '会话未就绪、即将到期或设备离线，请重新开启')
                if 'wave_rmt_v1' not in json.loads(s['telemetry']).get('capabilities', []):
                    raise HTTPException(409, '设备固件不支持波形，请先升级')
                if c.execute("SELECT 1 FROM wave_runs WHERE state IN ('arming','capturing') AND (receiver_session=? OR source_session=?)", (sid, sid)).fetchone():
                    raise HTTPException(409, '设备已有波形操作，请等待结束或取消')
                if c.execute("SELECT 1 FROM i2c_runs WHERE state IN ('arming','running','verifying') AND (master_session=? OR target_session=?)", (sid, sid)).fetchone():
                    raise HTTPException(409, '请先结束 I²C 事务')
                sessions.append(s)
            if len(sessions) == 2 and (sessions[0]['role'] == sessions[1]['role'] or sessions[0]['device_id'] == sessions[1]['device_id']):
                raise HTTPException(400, '输出与采集接口必须属于不同的配置设备')
            wid = secrets.token_hex(12)
            c.execute('INSERT INTO wave_runs(id,owner,request_id,receiver_session,source_session,mode,params,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (wid, owner, body.request_id, body.receiver_session, body.source_session, body.mode, json.dumps(params), 'arming', now, now+35, now))
            audit(c, 'wave_create', json.dumps({'id': wid, 'mode': body.mode, 'params': params, 'receiver': body.receiver_session, 'source': body.source_session}))
        return {'id': wid, 'state': 'arming'}

    @app.post('/api/waves/{wid}/cancel', dependencies=[Depends(admin)])
    def cancel(wid: str):
        with lock, db() as c:
            c.execute("UPDATE wave_runs SET state='cancelled',detail='管理员取消；设备收到控制后释放，有限输出最多持续 1 秒',updated=? WHERE id=? AND state IN ('arming','capturing')", (time.time(), wid))
            audit(c, 'wave_cancel', wid)
        return {'ok': True}

    @app.get('/api/waves/{wid}', dependencies=[Depends(admin)])
    def read(wid: str):
        with db() as c:
            row = c.execute('SELECT * FROM wave_runs WHERE id=?', (wid,)).fetchone()
            if not row:
                raise HTTPException(404, '波形不存在')
            result = dict(row)
            result.pop('owner')
            for key in ('params', 'stats', 'capture'):
                result[key] = json.loads(result[key])
            result['receiver'] = dict(c.execute('SELECT device_id,boot_id,role FROM debug_sessions WHERE id=?', (row['receiver_session'],)).fetchone())
            result['source'] = dict(c.execute('SELECT device_id,boot_id,role FROM debug_sessions WHERE id=?', (row['source_session'],)).fetchone()) if row['source_session'] else None
            return result

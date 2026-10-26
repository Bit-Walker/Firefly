"""Persistent, bounded UART RX logs. Readers neither ACK devices nor renew sessions."""
import base64
import json
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Query

from edbg_server.workspace import LOCAL_PRINCIPAL, reject


@dataclass(frozen=True)
class Retention:
    stream_records: int = 2048
    stream_bytes: int = 262144
    total_records: int = 65536
    total_bytes: int = 8388608


POLICY = Retention()


def identity(session_id):
    return 'uart-rx-' + session_id


def token(stream_id, position):
    raw = json.dumps([stream_id, position], separators=(',', ':')).encode()
    return 'c1.' + base64.urlsafe_b64encode(raw).decode().rstrip('=')


def position(cursor, stream_id):
    try:
        if len(cursor) > 512 or not cursor.startswith('c1.'):
            raise ValueError()
        data = base64.b64decode(cursor[3:] + '=' * (-len(cursor[3:]) % 4), altchars=b'-_', validate=True)
        value = json.loads(data)
        if not isinstance(value, list) or len(value) != 2 or value[0] != stream_id or type(value[1]) is not int or value[1] < 0:
            raise ValueError()
        return value[1]
    except (ValueError, UnicodeError, RecursionError):
        reject('CURSOR_INVALID', 'Cursor does not belong to this stream or has an invalid position.', 422)


def find(c, stream_id):
    row = c.execute('SELECT * FROM workspace_streams WHERE id=? AND principal=?', (stream_id, LOCAL_PRINCIPAL)).fetchone()
    if row is None:
        reject('STREAM_NOT_FOUND', 'Receive stream was not found.', 404)
    return row


def create(c, session_id, binding, principal=LOCAL_PRINCIPAL, origin='live', now=None):
    stream_id = identity(session_id)
    if c.execute('SELECT 1 FROM workspace_streams WHERE id=?', (stream_id,)).fetchone():
        return stream_id
    session = c.execute('SELECT role FROM debug_sessions WHERE id=?', (session_id,)).fetchone()
    channels = [1, 2] if session['role'] == 'probe' else [1]
    c.execute('''INSERT INTO workspace_streams(id,session_id,principal,node_id,boot_id,binding,channels,origin,created)
        VALUES(?,?,?,?,?,?,?,?,?)''', (stream_id, session_id, principal, binding['node_id'], binding['node_boot_id'],
                                      json.dumps(binding), json.dumps(channels), origin, time.time() if now is None else now))
    return stream_id


def append(c, stream_id, kind, document, byte_count=0):
    offset = c.execute('SELECT next_offset FROM workspace_streams WHERE id=?', (stream_id,)).fetchone()[0]
    c.execute('INSERT INTO workspace_stream_records(stream_id,offset,kind,document,byte_count) VALUES(?,?,?,?,?)',
              (stream_id, offset, kind, json.dumps(document, separators=(',', ':')), byte_count))
    c.execute('UPDATE workspace_streams SET next_offset=next_offset+1,retained_records=retained_records+1,retained_bytes=retained_bytes+? WHERE id=?', (byte_count, stream_id))
    c.execute('UPDATE workspace_stream_budget SET records=records+1,bytes=bytes+? WHERE id=1', (byte_count,))


def remove(c, rows):
    if not rows:
        return
    totals = {}
    for row in rows:
        count, size, floor = totals.get(row['stream_id'], (0, 0, 0))
        totals[row['stream_id']] = count+1, size+row['byte_count'], max(floor, row['offset'])
    for sid, (count, size, floor) in totals.items():
        c.execute('UPDATE workspace_streams SET retained_records=retained_records-?,retained_bytes=retained_bytes-?,trimmed_through=MAX(trimmed_through,?) WHERE id=?', (count, size, floor, sid))
    c.executemany('DELETE FROM workspace_stream_records WHERE global_id=?', [(row['global_id'],) for row in rows])
    c.execute('UPDATE workspace_stream_budget SET records=records-?,bytes=bytes-? WHERE id=1', (len(rows), sum(row['byte_count'] for row in rows)))


def trim(c, stream_id, policy=POLICY):
    row = c.execute('SELECT retained_records,retained_bytes FROM workspace_streams WHERE id=?', (stream_id,)).fetchone()
    count, size = row['retained_records'], row['retained_bytes']
    if count > policy.stream_records or size > policy.stream_bytes:
        candidates = c.execute('SELECT * FROM workspace_stream_records WHERE stream_id=? ORDER BY offset', (stream_id,)).fetchall()
        discarded = []
        for candidate in candidates:
            if count <= policy.stream_records and size <= policy.stream_bytes:
                break
            discarded.append(candidate); count -= 1; size -= candidate['byte_count']
        remove(c, discarded)
    while True:
        budget = c.execute('SELECT records,bytes FROM workspace_stream_budget WHERE id=1').fetchone()
        count, size = budget['records'], budget['bytes']
        if count <= policy.total_records and size <= policy.total_bytes:
            break
        candidates = c.execute('SELECT * FROM workspace_stream_records ORDER BY global_id LIMIT 256').fetchall()
        discarded = []
        for candidate in candidates:
            if count <= policy.total_records and size <= policy.total_bytes:
                break
            discarded.append(candidate); count -= 1; size -= candidate['byte_count']
        remove(c, discarded)


def ingest(c, session, snapshot, now, policy=POLICY):
    """Called only for a validated current session/boot, inside the device poll transaction."""
    stream_id = identity(session['id'])
    row = c.execute('SELECT * FROM workspace_streams WHERE id=?', (stream_id,)).fetchone()
    if row is None or row['ended']:
        return
    ack = row['acked_seq']
    channels = json.loads(row['channels'])
    for frame in sorted(snapshot.rx, key=lambda item: item.seq):
        if frame.seq <= ack:
            continue
        if frame.seq > ack + 1:
            append(c, stream_id, 'source_gap', {'received_at': now, 'from_seq': ack+1, 'to_seq': frame.seq-1,
                                               'missing_records': frame.seq-ack-1, 'affected_ports': None})
        if frame.port in channels:
            append(c, stream_id, 'rx', {'received_at': now, 'source_seq': frame.seq, 'port': frame.port,
                                        'source_time_us': frame.us, 'data_hex': frame.data.lower()}, len(frame.data)//2)
        else:
            append(c, stream_id, 'source_discarded', {'received_at': now, 'source_seq': frame.seq,
                'port': frame.port, 'reason': 'PORT_NOT_CONFIGURED'})
        ack = frame.seq
    dropped, errors = max(row['dropped'], snapshot.dropped), max(row['uart_errors'], snapshot.uart_errors)
    if dropped != row['dropped'] or errors != row['uart_errors']:
        append(c, stream_id, 'source_counters', {'received_at': now, 'dropped_total': dropped,
            'dropped_delta': dropped-row['dropped'], 'uart_errors_total': errors, 'uart_errors_delta': errors-row['uart_errors'],
            'overlaps_sequence_gaps': True})
    c.execute('UPDATE workspace_streams SET acked_seq=?,dropped=?,uart_errors=? WHERE id=?', (ack, dropped, errors, stream_id))
    trim(c, stream_id, policy)


def acknowledgement(c, session_id, fallback):
    row = c.execute('SELECT acked_seq FROM workspace_streams WHERE session_id=?', (session_id,)).fetchone()
    return max(fallback, row['acked_seq']) if row else fallback


def synchronize(c, node_id, now, policy=POLICY):
    for row in c.execute('''SELECT s.id,s.session_id,d.state FROM workspace_streams s JOIN debug_sessions d ON d.id=s.session_id
                            WHERE s.node_id=? AND s.ended=0 AND d.state IN ('closed','lost')''', (node_id,)).fetchall():
        opened = c.execute("SELECT published FROM workspace_operations WHERE session_id=? AND action='uart.session.open'", (row['session_id'],)).fetchone()
        evidence = 'legacy_state' if opened is None else 'server_never_published' if not opened['published'] else 'node_boot_changed' if row['state']=='lost' else 'node_session_end'
        append(c, row['id'], 'source_ended', {'received_at': now, 'reason': row['state'], 'end_evidence':evidence, 'tail_complete': False})
        c.execute('UPDATE workspace_streams SET ended=1 WHERE id=?', (row['id'],))
        trim(c, row['id'], policy)


def import_existing(c, now=None):
    if c.execute("SELECT 1 FROM workspace_imports WHERE name='uart-rx-streams-v1'").fetchone():
        return
    now = time.time() if now is None else now
    nodes = set()
    for managed in c.execute('SELECT * FROM workspace_uart_sessions').fetchall():
        binding = json.loads(managed['binding'])
        sid = managed['session_id']; nodes.add(binding['node_id'])
        if c.execute('SELECT 1 FROM workspace_streams WHERE session_id=?', (sid,)).fetchone():
            continue
        stream_id = create(c, sid, binding, managed['principal'], 'legacy_backfill', now)
        append(c, stream_id, 'history_unavailable', {'received_at': now, 'reason': 'Only retained legacy frames are available; prefix completeness is unknown.'})
        high = 0
        for frame in c.execute('SELECT * FROM debug_rx WHERE session_id=? ORDER BY seq', (sid,)).fetchall():
            if frame['seq'] > high+1:
                append(c, stream_id, 'history_unavailable', {'received_at': now, 'from_seq': high+1, 'to_seq': frame['seq']-1})
            append(c, stream_id, 'rx', {'received_at': frame['created'], 'source_seq': frame['seq'], 'port': frame['port'],
                'source_time_us': frame['us'], 'data_hex': frame['data'], 'historical': True}, len(frame['data'])//2)
            high = frame['seq']
        snapshot = json.loads(c.execute('SELECT snapshot FROM debug_sessions WHERE id=?', (sid,)).fetchone()[0])
        dropped, errors = snapshot.get('dropped',0), snapshot.get('uart_errors',0)
        if dropped or errors:
            append(c, stream_id, 'source_counters', {'received_at':now,'dropped_total':dropped,'uart_errors_total':errors,
                'historical':True,'overlaps_sequence_gaps':True})
        c.execute('UPDATE workspace_streams SET acked_seq=?,dropped=?,uart_errors=? WHERE id=?', (high,dropped,errors,stream_id))
        trim(c, stream_id)
    for node in nodes:
        synchronize(c, node, now)
    c.execute("INSERT INTO workspace_imports VALUES('uart-rx-streams-v1',?)", (now,))


def metadata(c, row):
    session = c.execute('SELECT state,expires FROM debug_sessions WHERE id=?', (row['session_id'],)).fetchone()
    return {'api_version': 'edbg.stream/v0', **{key: row[key] for key in ('id','session_id','node_id','boot_id','origin','created')},
            'direction': 'rx', 'channels': json.loads(row['channels']), 'binding': json.loads(row['binding']),
            'session_state': session['state'], 'session_expires': session['expires'], 'source_ended': bool(row['ended']),
            'origin_at_session_start': row['origin']=='live', 'retained_from_origin': row['trimmed_through']==0,
            'tail_complete': False,
            'source_checkpoint': row['acked_seq'], 'source_counters': {'dropped': row['dropped'], 'uart_errors': row['uart_errors']},
            'counter_history_complete': row['origin']=='live',
            'limits': {'stream_records':POLICY.stream_records,'stream_payload_bytes':POLICY.stream_bytes,
                       'total_records':POLICY.total_records,'total_payload_bytes':POLICY.total_bytes},
            'retention': {'records': row['retained_records'], 'payload_bytes': row['retained_bytes'],
                'trimmed_through': row['trimmed_through'], 'earliest_after': token(row['id'], row['trimmed_through']),
                'latest_after': token(row['id'], row['next_offset']-1)}}


def read(c, stream_id, after=None, limit=100, limit_bytes=4096, port=None):
    row = find(c, stream_id)
    meta = metadata(c, row)
    start = row['trimmed_through'] if after is None else position(after, stream_id)
    if start < row['trimmed_through']:
        raise HTTPException(410, {'code': 'CURSOR_EXPIRED', 'message': 'Requested stream prefix has been removed by retention.',
                                  'context': {'stream_id': stream_id, **{key:meta['retention'][key] for key in ('earliest_after','latest_after','trimmed_through')}}})
    if start >= row['next_offset']:
        reject('CURSOR_INVALID', 'Cursor is ahead of the committed stream.', 422)
    if port is not None and port not in json.loads(row['channels']):
        reject('STREAM_PORT_UNAVAILABLE', 'Requested port is not part of this stream.', 422)
    rows = c.execute('SELECT * FROM workspace_stream_records WHERE stream_id=? AND offset>? ORDER BY offset LIMIT ?', (stream_id, start, limit+1)).fetchall()
    items, size, consumed = [], 0, start
    for item in rows[:limit]:
        document = json.loads(item['document'])
        included = item['kind'] != 'rx' or port is None or document['port'] == port
        if included and size + item['byte_count'] > limit_bytes:
            break
        consumed = item['offset']
        if included:
            size += item['byte_count']
            items.append({'offset': item['offset'], 'kind': item['kind'], **document})
    return {'api_version': 'edbg.stream.read/v0', 'stream': meta, 'items': items,
            'next_after': token(stream_id, consumed), 'payload_bytes': size, 'scanned_records': consumed-start,
            'has_more': consumed < row['next_offset']-1, 'eof': bool(row['ended']) and consumed == row['next_offset']-1,
            'history_truncated': row['trimmed_through'] > 0, 'port': port}


def register(app, db, admin):
    @app.get('/api/workspace/streams', dependencies=[Depends(admin)])
    def listing(node: str = '', session: str = '', before: str = '', limit: int = Query(default=50, ge=1, le=200)):
        with db() as c:
            c.execute('BEGIN')
            where, args = 'principal=?', [LOCAL_PRINCIPAL]
            for column, value in (('node_id', node), ('session_id', session)):
                if value:
                    where += ' AND '+column+'=?'; args.append(value)
            if before:
                cursor = find(c, before)
                where += ' AND (created,id)<(?,?)'; args.extend((cursor['created'], cursor['id']))
            rows = c.execute('SELECT * FROM workspace_streams WHERE '+where+' ORDER BY created DESC,id DESC LIMIT ?', (*args, limit+1)).fetchall()
            return {'items': [metadata(c,r) for r in rows[:limit]], 'next_before': rows[limit-1]['id'] if len(rows)>limit else None}

    @app.get('/api/workspace/streams/{stream_id}', dependencies=[Depends(admin)])
    def show(stream_id: str):
        with db() as c:
            c.execute('BEGIN')
            return metadata(c, find(c, stream_id))

    @app.get('/api/workspace/streams/{stream_id}/read', dependencies=[Depends(admin)])
    def reading(stream_id: str, after: str | None = None, limit: int = Query(default=100, ge=1, le=200),
                limit_bytes: int = Query(default=4096, ge=128, le=65536), port: int | None = Query(default=None, ge=1, le=2)):
        with db() as c:
            c.execute('BEGIN')
            return read(c, stream_id, after, limit, limit_bytes, port)

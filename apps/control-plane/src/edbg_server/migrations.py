"""Numbered additive migrations, serialized across both local API processes."""
import hashlib
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path

MIGRATIONS = [(1, 'versioned-workspace', (
    '''CREATE TABLE workspace_targets (
       id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
       source TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(id,revision))''',
    '''CREATE TABLE workspace_connections (
       id TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
       source TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(id,revision))''',
    'CREATE TABLE workspace_imports (name TEXT PRIMARY KEY, created REAL NOT NULL)',
)), (2, 'uart-operation-ledger', (
    '''CREATE TABLE workspace_uart_sessions (
       session_id TEXT PRIMARY KEY REFERENCES debug_sessions(id), principal TEXT NOT NULL,
       connection_id TEXT NOT NULL, connection_revision INTEGER NOT NULL, binding TEXT NOT NULL)''',
    '''CREATE TABLE workspace_operations (
       id TEXT PRIMARY KEY, principal TEXT NOT NULL, idempotency_key TEXT NOT NULL,
       request TEXT NOT NULL, request_digest TEXT NOT NULL, action TEXT NOT NULL,
       node_id TEXT NOT NULL, binding TEXT NOT NULL, resources TEXT NOT NULL,
       session_id TEXT NOT NULL REFERENCES debug_sessions(id), tx_id TEXT REFERENCES debug_tx(id),
       state TEXT NOT NULL, outcome TEXT, resource_status TEXT NOT NULL,
       result TEXT NOT NULL DEFAULT '{}', evidence TEXT NOT NULL DEFAULT '[]',
       created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
       dispatch_before REAL NOT NULL, execution_ms INTEGER NOT NULL, execution_deadline REAL,
       published INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
       UNIQUE(principal,idempotency_key))''',
    '''CREATE TABLE workspace_operation_events (
       operation_id TEXT NOT NULL REFERENCES workspace_operations(id), seq INTEGER NOT NULL,
       created REAL NOT NULL, document TEXT NOT NULL, PRIMARY KEY(operation_id,seq))''',
    'CREATE INDEX workspace_operation_pending ON workspace_operations(node_id,state)',
    'CREATE UNIQUE INDEX workspace_operation_tx ON workspace_operations(tx_id) WHERE tx_id IS NOT NULL',
)), (3, 'uart-receive-streams', (
    '''CREATE TABLE workspace_streams (
       id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE REFERENCES debug_sessions(id),
       principal TEXT NOT NULL, node_id TEXT NOT NULL, boot_id TEXT NOT NULL, binding TEXT NOT NULL,
       channels TEXT NOT NULL, origin TEXT NOT NULL, created REAL NOT NULL,
       next_offset INTEGER NOT NULL DEFAULT 1, trimmed_through INTEGER NOT NULL DEFAULT 0,
       acked_seq INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0,
       uart_errors INTEGER NOT NULL DEFAULT 0, retained_records INTEGER NOT NULL DEFAULT 0,
       retained_bytes INTEGER NOT NULL DEFAULT 0, ended INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE workspace_stream_records (
       global_id INTEGER PRIMARY KEY AUTOINCREMENT,
       stream_id TEXT NOT NULL REFERENCES workspace_streams(id), offset INTEGER NOT NULL,
       kind TEXT NOT NULL, document TEXT NOT NULL, byte_count INTEGER NOT NULL,
       UNIQUE(stream_id,offset))''',
    '''CREATE TABLE workspace_stream_budget (
       id INTEGER PRIMARY KEY CHECK(id=1), records INTEGER NOT NULL, bytes INTEGER NOT NULL)''',
    'INSERT INTO workspace_stream_budget VALUES(1,0,0)',
    'CREATE INDEX workspace_stream_node ON workspace_streams(node_id,created)',
    'CREATE INDEX workspace_stream_pending_end ON workspace_streams(node_id) WHERE ended=0',
)), (4, 'uart-lease-revisions', (
    'ALTER TABLE workspace_uart_sessions ADD COLUMN lease_revision INTEGER NOT NULL DEFAULT 1',
)), (5, 'finite-i2c-operations', (
    'ALTER TABLE i2c_runs ADD COLUMN workspace_operation_id TEXT REFERENCES workspace_operations(id)',
    '''CREATE TABLE workspace_i2c_operations (
       operation_id TEXT PRIMARY KEY REFERENCES workspace_operations(id),
       session_id TEXT NOT NULL REFERENCES debug_sessions(id),
       run_id TEXT REFERENCES i2c_runs(id), bus_key TEXT NOT NULL,
       context_published INTEGER NOT NULL DEFAULT 0, held INTEGER NOT NULL DEFAULT 1,
       bus_outcome TEXT, bus_code TEXT NOT NULL DEFAULT '', bus_report TEXT NOT NULL DEFAULT '{}',
       cleanup_deadline REAL)''',
    'CREATE UNIQUE INDEX workspace_i2c_held ON workspace_i2c_operations(bus_key) WHERE held=1',
    'CREATE INDEX workspace_i2c_context ON workspace_i2c_operations(session_id)',
)), (6, 'node-update-operations', (
    # Rebuild with the same identity while migration's private connection has FKs
    # disabled. Children retain REFERENCES workspace_operations(id).
    '''CREATE TABLE workspace_operations_next (
       id TEXT PRIMARY KEY, principal TEXT NOT NULL, idempotency_key TEXT NOT NULL,
       request TEXT NOT NULL, request_digest TEXT NOT NULL, action TEXT NOT NULL,
       node_id TEXT NOT NULL, binding TEXT NOT NULL, resources TEXT NOT NULL,
       session_id TEXT REFERENCES debug_sessions(id), tx_id TEXT REFERENCES debug_tx(id),
       state TEXT NOT NULL, outcome TEXT, resource_status TEXT NOT NULL,
       result TEXT NOT NULL DEFAULT '{}', evidence TEXT NOT NULL DEFAULT '[]',
       created REAL NOT NULL, updated REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
       dispatch_before REAL NOT NULL, execution_ms INTEGER NOT NULL, execution_deadline REAL,
       published INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0,
       UNIQUE(principal,idempotency_key))''',
    'INSERT INTO workspace_operations_next SELECT * FROM workspace_operations',
    'DROP TABLE workspace_operations',
    'ALTER TABLE workspace_operations_next RENAME TO workspace_operations',
    'CREATE INDEX workspace_operation_pending ON workspace_operations(node_id,state)',
    'CREATE UNIQUE INDEX workspace_operation_tx ON workspace_operations(tx_id) WHERE tx_id IS NOT NULL',
    '''CREATE TABLE workspace_update_operations (
       operation_id TEXT PRIMARY KEY REFERENCES workspace_operations(id),
       job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id), node_id TEXT NOT NULL REFERENCES devices(id),
       held INTEGER NOT NULL DEFAULT 1, report TEXT NOT NULL DEFAULT '{}',
       report_boot TEXT, report_version TEXT, report_partition TEXT)''',
    'CREATE UNIQUE INDEX workspace_update_held ON workspace_update_operations(node_id) WHERE held=1',
)), (7, 'runtime-image-evidence', (
    "ALTER TABLE workspace_update_operations ADD COLUMN report_runtime TEXT NOT NULL DEFAULT '{}'",
))]


def migrate(path):
    path = Path(path)
    c = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        c.execute('PRAGMA foreign_keys=OFF')
        c.execute('BEGIN IMMEDIATE')
        has_ledger = c.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_migrations'").fetchone()
        applied = dict(c.execute('SELECT version,checksum FROM workspace_migrations')) if has_ledger else {}
        if set(applied) - {version for version, _, _ in MIGRATIONS}:
            raise RuntimeError('Workspace database is newer than this service')
        pending = []
        for version, name, statements in MIGRATIONS:
            checksum = hashlib.sha256('\n'.join(statements).encode()).hexdigest()
            if version in applied and applied[version] != checksum:
                raise RuntimeError('Applied workspace migration checksum differs from source')
            if version not in applied:
                pending.append((version, name, statements, checksum))
        if pending:
            # Read from a separate connection while this transaction holds the writer lock.
            # Do not use executescript: its implicit commit would break migration atomicity.
            backup_path = path.parent / f'migration-{pending[0][0]:04d}-{secrets.token_hex(4)}.local.bak'
            with closing(sqlite3.connect(path, timeout=30)) as snapshot, closing(sqlite3.connect(backup_path)) as backup:
                snapshot.backup(backup)
            c.execute('''CREATE TABLE IF NOT EXISTS workspace_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied REAL NOT NULL)''')
            for version, name, statements, checksum in pending:
                for statement in statements:
                    c.execute(statement)
                if version==6 and c.execute('PRAGMA foreign_key_check').fetchall():
                    raise RuntimeError('Foreign-key integrity check failed during operation table migration')
                c.execute('INSERT INTO workspace_migrations VALUES(?,?,?,?)', (version, name, checksum, time.time()))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()

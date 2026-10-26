import concurrent.futures
import contextlib
import io
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from edbg_sdk import Client
from edbg_cli.main import main
from . import agent_runtime

from fastapi.testclient import TestClient

temporary = tempfile.TemporaryDirectory()
previous_state = os.environ.get('FLEET_STATE')
os.environ['FLEET_STATE'] = temporary.name
from edbg_server import app as server, migrations, planning, workspace
if previous_state is None:
    os.environ.pop('FLEET_STATE', None)
else:
    os.environ['FLEET_STATE'] = previous_state


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app)
        self.client.headers['Origin'] = 'http://testserver'
        self.client.post('/api/login', json=server.ADMIN).raise_for_status()
        self.did = 's3-' + secrets.token_hex(6)
        self.client.post('/api/devices', json={'id': self.did, 'name': 'Isolated test node'}).raise_for_status()
        server.debug_api.BENCH[self.did] = 'target'
        with server.db() as c:
            c.execute('UPDATE devices SET boot_id=?,last_seen=?,version=?,telemetry=? WHERE id=?',
                      ('test-boot', time.time(), '3.5.0', json.dumps({'capabilities': ['debug_uart_v1', 'i2c_master_v2'],'runtime':agent_runtime()}), self.did))
        self.target = {'api_version': 'edbg.target/v0', 'id': 'target-' + self.did, 'name': 'Test target', 'kind': 'self', 'node_id': self.did}
        self.apply('targets', self.target, 0).raise_for_status()
        self.connection = {'api_version': 'edbg.connection/v0', 'id': 'uart-' + self.did, 'name': 'UART', 'node_id': self.did,
            'target': {'id': self.target['id'], 'revision': 1}, 'interface': {'id': 'uart1', 'kind': 'uart'}, 'adapter_id': 'esp32s3-gpio-v1'}
        self.apply('connections', self.connection, 0).raise_for_status()
        fixture = Path(planning.SCHEMA_PATH).parent / 'fixtures/uart-open.json'
        self.request = json.loads(fixture.read_text(encoding='utf-8'))
        self.request['connection'] = {'id': self.connection['id'], 'revision': 1}

    def tearDown(self):
        server.debug_api.BENCH.pop(self.did, None)
        self.client.close()

    def apply(self, kind, configuration, expected):
        return self.client.put(f'/api/workspace/{kind}/{configuration["id"]}',
                               json={'configuration': configuration, 'expected_revision': expected})

    def plan(self):
        response = self.client.post('/api/workspace/operation-plan', json=self.request)
        response.raise_for_status()
        return response.json()

    def test_authentication_origin_and_strict_configuration(self):
        self.assertEqual(TestClient(server.app).get('/api/workspace/connections').status_code, 401)
        self.assertEqual(self.client.put('/api/workspace/targets/x', json={}, headers={'Origin': 'https://wrong.invalid'}).status_code, 403)
        data = dict(self.connection, allow_any_gpio=True)
        self.assertEqual(self.apply('connections', data, 1).status_code, 422)
        self.assertEqual(self.client.put('/api/workspace/connections/different', json={'configuration': self.connection, 'expected_revision': 1}).status_code, 422)
        self.assertEqual(self.apply('connections', self.connection, True).status_code, 422)

    def test_compare_and_swap_and_pinned_target_history(self):
        changed = dict(self.target, name='Renamed')
        self.assertEqual(self.apply('targets', changed, 1).json()['revision'], 2)
        self.assertEqual(self.apply('targets', changed, 1).status_code, 409)
        historical = self.client.get(f'/api/workspace/targets/{self.target["id"]}?revision=1').json()
        self.assertEqual(historical['name'], 'Test target')
        self.assertEqual(self.plan()['binding']['target']['revision'], 1)
        changed_connection = dict(self.connection, enabled=False)
        self.assertEqual(self.apply('connections', changed_connection, 1).json()['revision'], 2)
        self.assertIn('CONNECTION_REVISION_MISMATCH', [r['code'] for r in self.plan()['reasons']])
        self.request['connection']['revision'] = 2
        self.assertIn('CONNECTION_DISABLED', [r['code'] for r in self.plan()['reasons']])

    def test_concurrent_configuration_updates_have_one_winner(self):
        def update(name):
            with TestClient(server.app) as client:
                client.headers.update(self.client.headers)
                client.cookies.update(self.client.cookies)
                return client.put(f'/api/workspace/connections/{self.connection["id"]}',
                    json={'configuration': dict(self.connection, name=name), 'expected_revision': 1}).status_code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ('one', 'two')))
        self.assertEqual(sorted(results), [200, 409])

    def test_new_instances_need_configuration_but_do_not_bypass_firmware_guard(self):
        server.debug_api.BENCH.pop(self.did)
        result = self.plan()
        self.assertEqual(result['status'], 'unsupported')
        self.assertIn('FIRMWARE_IDENTITY_RESTRICTED', [r['code'] for r in result['reasons']])

    def test_plan_is_supported_without_dispatch_or_any_database_changes(self):
        def snapshot():
            with server.db() as c:
                return '\n'.join(c.iterdump())
        before = snapshot()
        result = self.plan()
        self.assertEqual(result['status'], 'supported')
        self.assertFalse(result['reserved']); self.assertTrue(result['submission_available'])
        self.assertIn(f'node:{self.did}:legacy-debug', [r['id'] for r in result['resources']])
        self.assertEqual(before, snapshot())

    def test_offline_boot_guard_unsupported_parameters_and_unknown_adapter(self):
        self.request['preconditions'] = {'node_boot_id': 'different'}
        self.assertIn('BOOT_CONTEXT_CHANGED', [r['code'] for r in self.plan()['reasons']])
        self.request.pop('preconditions')
        with server.db() as c:
            c.execute('UPDATE devices SET last_seen=0 WHERE id=?', (self.did,))
        self.assertIn('NODE_OFFLINE', [r['code'] for r in self.plan()['reasons']])
        self.request['params']['baud_rate'] = 9600
        self.assertIn('UART_PARAMETERS_UNSUPPORTED', [r['code'] for r in self.plan()['reasons']])
        changed = dict(self.connection, adapter_id='future-provider')
        self.apply('connections', changed, 1).raise_for_status()
        self.request['connection']['revision'] = 2
        self.assertIn('ADAPTER_NOT_INSTALLED', [r['code'] for r in self.plan()['reasons']])

    def test_expired_context_still_blocks_until_release_is_known(self):
        now = time.time()
        with server.db() as c:
            c.execute('INSERT INTO debug_sessions(id,device_id,role,boot_id,owner,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                      (secrets.token_hex(12), self.did, 'target', 'test-boot', 'other-owner', 'expired', now-20, now-10, now))
        self.assertIn('DEBUG_CONTEXT_OCCUPIED', [r['code'] for r in self.plan()['reasons']])

    def test_strict_plan_input_rejects_duplicates_nonfinite_floats_and_oversize(self):
        for raw in ('{"action":"one","action":"two"}', '{"value":NaN}', json.dumps(self.request).replace('115200', '115200.0')):
            self.assertEqual(self.client.post('/api/workspace/operation-plan', content=raw).status_code, 422)
        self.assertEqual(self.client.post('/api/workspace/operation-plan', content=b' ' * 262145).status_code, 413)

    def test_i2c_resources_share_physical_bus_and_keep_legacy_global_lock(self):
        connection = dict(self.connection, interface={'id': 'i2c0', 'kind': 'i2c'}, physical_bus_id='shared-test-bus')
        self.apply('connections', connection, 1).raise_for_status()
        fixture = Path(planning.SCHEMA_PATH).parent / 'fixtures/i2c-register-read.json'
        self.request = json.loads(fixture.read_text(encoding='utf-8'))
        self.request['connection'] = {'id': self.connection['id'], 'revision': 2}
        result = self.plan()
        self.assertEqual(result['status'], 'supported')
        self.assertIn('bus:shared-test-bus', [r['id'] for r in result['resources']])
        self.assertIn('adapter:legacy-i2c-global', [r['id'] for r in result['resources']])
        self.request['params']['messages'].append({'read_length': 1})
        self.assertIn('I2C_PARAMETERS_UNSUPPORTED', [r['code'] for r in self.plan()['reasons']])

    def test_foreign_session_cannot_be_claimed_by_plan(self):
        now = time.time(); sid = secrets.token_hex(12)
        with server.db() as c:
            c.execute('INSERT INTO debug_sessions(id,device_id,role,boot_id,owner,state,created,expires,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                      (sid, self.did, 'target', 'test-boot', 'other-owner', 'active', now, now+100, now))
        self.request.update(action='uart.write', params={'session_id': sid, 'data_hex': '00'})
        self.assertIn('SESSION_CONTROL_NOT_HELD', [r['code'] for r in self.plan()['reasons']])

    def test_sdk_and_cli_configuration_and_plan_use_real_api(self):
        factory = lambda url, **kw: Client('http://testserver', http_client=self.client)
        def invoke(arguments):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main([*arguments, '--json'], client_factory=factory)
            return code, json.loads(output.getvalue())
        code, result = invoke(['connection', 'show', self.connection['id']])
        self.assertEqual(code, 0); self.assertEqual(result['data']['revision'], 1)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'connection.json'
            path.write_text(json.dumps(dict(self.connection, name='Changed through CLI')), encoding='utf-8')
            code, result = invoke(['connection', 'apply', '--file', str(path), '--expect-revision', '1'])
            self.assertEqual(code, 0); self.assertEqual(result['data']['revision'], 2)
            code, result = invoke(['connection', 'apply', '--file', str(path), '--expect-revision', '1'])
            self.assertEqual(code, 7); self.assertEqual(result['error']['code'], 'REVISION_CONFLICT')
            path.write_text(json.dumps(self.request), encoding='utf-8')
            code, result = invoke(['operation', 'plan', '--file', str(path)])
            self.assertEqual(code, 0); self.assertEqual(result['data']['status'], 'blocked')
            self.assertFalse(result['data']['reserved'])
        code, result = invoke(['target', 'list'])
        self.assertEqual(code, 0); self.assertIn(self.target['id'], [t['id'] for t in result['data']])

    def test_legacy_import_is_atomic_and_does_not_duplicate_or_overwrite_revisions(self):
        with server.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("DELETE FROM workspace_imports WHERE name='legacy-bench-v1'")
            workspace.import_legacy(c, {self.did: 'target'})
            imported = workspace.record(c, 'connection', 'agent-' + self.did)
            self.assertEqual(imported['source'], 'legacy_import')
            count = c.execute('SELECT COUNT(*) FROM workspace_connections').fetchone()[0]
            workspace.import_legacy(c, {self.did: 'target'})
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_connections').fetchone()[0], count)

    def test_missing_node_target_and_maintenance_target_mismatch_are_rejected(self):
        configuration = dict(self.connection, target={'id': 'missing-target', 'revision': 1})
        self.assertEqual(self.apply('connections', configuration, 1).status_code, 422)
        configuration = dict(self.connection, node_id='missing-node')
        self.assertEqual(self.apply('connections', configuration, 1).status_code, 422)
        external = dict(self.target, id='external-' + self.did, kind='external', node_id=None)
        self.apply('targets', external, 0).raise_for_status()
        configuration = dict(self.connection, target={'id': external['id'], 'revision': 1}, interface={'id': 'agent', 'kind': 'maintenance'})
        self.assertEqual(self.apply('connections', configuration, 1).status_code, 422)

    def test_failed_import_rolls_back_partial_rows_and_import_marker(self):
        with server.db() as c:
            before = c.execute('SELECT COUNT(*) FROM workspace_connections').fetchone()[0]
        original = workspace.save
        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('Simulated import interruption')
        with self.assertRaises(RuntimeError):
            with server.db() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("DELETE FROM workspace_imports WHERE name='legacy-bench-v1'")
                with patch.object(workspace, 'save', fail_after_write):
                    workspace.import_legacy(c, {self.did: 'target'})
        with server.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_connections').fetchone()[0], before)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM workspace_imports WHERE name='legacy-bench-v1'").fetchone()[0], 1)


class MigrationTests(unittest.TestCase):
    def legacy_database(self, root):
        path = Path(root)/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as c, c:
            c.execute('CREATE TABLE existing_data (value TEXT)')
            c.execute('CREATE TABLE i2c_runs (id TEXT PRIMARY KEY)')
            for name in ('devices','jobs','debug_sessions','debug_tx'):
                c.execute('CREATE TABLE '+name+' (id TEXT PRIMARY KEY)')
            c.execute("INSERT INTO existing_data VALUES('preserved')")
        return path

    def test_parallel_migration_runs_once_and_backup_preserves_pre_migration_state(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.legacy_database(root)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(migrations.migrate, (path, path)))
            migrations.migrate(path)
            backups = list(Path(root).glob('*.local.bak'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as c:
                self.assertEqual(c.execute('SELECT value FROM existing_data').fetchone()[0], 'preserved')
                self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_migrations'").fetchone())
            with closing(sqlite3.connect(path)) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM workspace_migrations').fetchone()[0], len(migrations.MIGRATIONS))

    def test_failed_migration_rolls_back_all_ddl_and_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.legacy_database(root)
            with patch.object(migrations, 'MIGRATIONS', [(1, 'broken', ('CREATE TABLE must_rollback(x)', 'INVALID SQL'))]):
                with self.assertRaises(sqlite3.Error):
                    migrations.migrate(path)
            with closing(sqlite3.connect(path)) as c:
                self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name IN ('must_rollback','workspace_migrations')").fetchone())
                self.assertEqual(c.execute('SELECT value FROM existing_data').fetchone()[0], 'preserved')

    def test_changed_or_future_migration_history_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            path = self.legacy_database(root)
            migrations.migrate(path)
            with patch.object(migrations, 'MIGRATIONS', [(1, 'changed', ('CREATE TABLE different(x)',))]):
                with self.assertRaises(RuntimeError): migrations.migrate(path)
            with closing(sqlite3.connect(path)) as c, c:
                c.execute("INSERT INTO workspace_migrations VALUES(?,'future','digest',0)", (max(v for v, _, _ in migrations.MIGRATIONS)+1,))
            with self.assertRaises(RuntimeError): migrations.migrate(path)


if __name__ == '__main__':
    unittest.main()

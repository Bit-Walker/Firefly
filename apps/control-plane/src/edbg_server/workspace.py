"""Append-only configuration revisions; declarations never authorize hardware execution."""
import json
import time
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$')]
LOCAL_PRINCIPAL = 'local-admin'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Target(StrictModel):
    api_version: Literal['edbg.target/v0']
    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    kind: Literal['self', 'external']
    node_id: Identifier | None = None

    @model_validator(mode='after')
    def consistent(self):
        if (self.kind == 'self') != (self.node_id is not None):
            raise ValueError('Self targets require node_id; external targets must omit it')
        return self


class TargetRef(StrictModel):
    id: Identifier
    revision: int = Field(ge=1)


class Interface(StrictModel):
    id: Identifier
    kind: Literal['uart', 'i2c', 'maintenance']


class Connection(StrictModel):
    api_version: Literal['edbg.connection/v0']
    id: Identifier
    name: str = Field(min_length=1, max_length=120)
    node_id: Identifier
    target: TargetRef
    interface: Interface
    adapter_id: Identifier
    physical_bus_id: Identifier | None = None
    enabled: bool = True

    @model_validator(mode='after')
    def consistent(self):
        if (self.interface.kind == 'i2c') != (self.physical_bus_id is not None):
            raise ValueError('Only I2C connections require physical_bus_id')
        return self


class TargetUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    configuration: Target


class ConnectionUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    configuration: Connection


def table(kind):
    return {'target': 'workspace_targets', 'connection': 'workspace_connections'}[kind]


def record(c, kind, identity, revision=None):
    query = f'SELECT * FROM {table(kind)} WHERE id=?'
    args = [identity]
    if revision is not None:
        query += ' AND revision=?'
        args.append(revision)
    row = c.execute(query + ' ORDER BY revision DESC LIMIT 1', args).fetchone()
    return None if row is None else {**json.loads(row['document']), 'revision': row['revision'],
                                    'source': row['source'], 'created': row['created']}


def inventory(c, kind):
    rows = c.execute(f'SELECT id,MAX(revision) FROM {table(kind)} GROUP BY id ORDER BY id').fetchall()
    return [record(c, kind, row[0], row[1]) for row in rows]


def reject(code, message, status=409):
    raise HTTPException(status, {'code': code, 'message': message})


def save(c, kind, configuration, expected, source='operator_config'):
    current = record(c, kind, configuration['id'])
    revision = current['revision'] if current else 0
    if revision != expected:
        reject('REVISION_CONFLICT', 'Configuration changed; read its current revision before updating.')
    c.execute(f'INSERT INTO {table(kind)} VALUES(?,?,?,?,?)',
              (configuration['id'], revision + 1, json.dumps(configuration, sort_keys=True), source, time.time()))
    return record(c, kind, configuration['id'])


def require_node(c, identity):
    node = c.execute('SELECT id,revoked FROM devices WHERE id=?', (identity,)).fetchone()
    if not node or node['revoked']:
        reject('NODE_NOT_REGISTERED', 'Configuration requires a registered, non-revoked node.', 422)


def import_legacy(c, bench):
    """One-time metadata import, only for devices already registered at migration time."""
    if c.execute("SELECT 1 FROM workspace_imports WHERE name='legacy-bench-v1'").fetchone():
        return
    nodes = [dict(row) for row in c.execute('SELECT id,name FROM devices WHERE revoked=0') if row['id'] in bench]
    if nodes and record(c, 'target', 'legacy-i2c-target') is None:
        save(c, 'target', {'api_version': 'edbg.target/v0', 'id': 'legacy-i2c-target',
                          'name': 'Legacy I2C wiring; target not identified', 'kind': 'external', 'node_id': None}, 0, 'legacy_import')
    for node in nodes:
        did = node['id']
        target_id = 'self-' + did
        if record(c, 'target', target_id) is None:
            save(c, 'target', {'api_version': 'edbg.target/v0', 'id': target_id, 'name': node['name'],
                              'kind': 'self', 'node_id': did}, 0, 'legacy_import')
        for prefix, interface, kind in [('uart', 'uart1', 'uart'), ('i2c', 'i2c0', 'i2c'), ('agent', 'agent', 'maintenance')]:
            identity = prefix + '-' + did
            if record(c, 'connection', identity) is not None:
                continue
            target = 'legacy-i2c-target' if kind == 'i2c' else target_id
            configuration = {'api_version': 'edbg.connection/v0', 'id': identity, 'name': node['name'] + ' / ' + prefix,
                'node_id': did, 'target': {'id': target, 'revision': record(c, 'target', target)['revision']},
                'interface': {'id': interface, 'kind': kind}, 'adapter_id': 'esp32s3-gpio-v1',
                'physical_bus_id': 'legacy-i2c-bus' if kind == 'i2c' else None, 'enabled': True}
            save(c, 'connection', configuration, 0, 'legacy_import')
    c.execute("INSERT INTO workspace_imports VALUES('legacy-bench-v1',?)", (time.time(),))


def register(app, db, admin, audit):
    @app.get('/api/workspace/targets', dependencies=[Depends(admin)])
    def targets():
        with db() as c:
            c.execute('BEGIN')
            return inventory(c, 'target')

    @app.get('/api/workspace/targets/{identity}', dependencies=[Depends(admin)])
    def target(identity: str, revision: int | None = None):
        with db() as c:
            item = record(c, 'target', identity, revision)
            if item is None:
                reject('TARGET_NOT_FOUND', 'Target revision was not found.', 404)
            return item

    @app.put('/api/workspace/targets/{identity}', dependencies=[Depends(admin)])
    def put_target(identity: str, body: TargetUpdate):
        if identity != body.configuration.id:
            reject('IDENTITY_MISMATCH', 'Path and configuration identities must match.', 422)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            if body.configuration.node_id:
                require_node(c, body.configuration.node_id)
            result = save(c, 'target', body.configuration.model_dump(), body.expected_revision)
            audit(c, 'target_configuration', identity + ':' + str(result['revision']))
            return result

    @app.get('/api/workspace/connections', dependencies=[Depends(admin)])
    def connections():
        with db() as c:
            c.execute('BEGIN')
            return inventory(c, 'connection')

    @app.get('/api/workspace/connections/{identity}', dependencies=[Depends(admin)])
    def connection(identity: str, revision: int | None = None):
        with db() as c:
            item = record(c, 'connection', identity, revision)
            if item is None:
                reject('CONNECTION_NOT_FOUND', 'Connection revision was not found.', 404)
            return item

    @app.put('/api/workspace/connections/{identity}', dependencies=[Depends(admin)])
    def put_connection(identity: str, body: ConnectionUpdate):
        if identity != body.configuration.id:
            reject('IDENTITY_MISMATCH', 'Path and configuration identities must match.', 422)
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            require_node(c, body.configuration.node_id)
            ref = body.configuration.target
            target = record(c, 'target', ref.id, ref.revision)
            if target is None:
                reject('TARGET_NOT_FOUND', 'Referenced target revision does not exist.', 422)
            if body.configuration.interface.kind == 'maintenance' and (target['kind'] != 'self' or target['node_id'] != body.configuration.node_id):
                reject('TARGET_MISMATCH', 'Maintenance must reference this node as a self target.', 422)
            result = save(c, 'connection', body.configuration.model_dump(), body.expected_revision)
            audit(c, 'connection_configuration', identity + ':' + str(result['revision']))
            return result

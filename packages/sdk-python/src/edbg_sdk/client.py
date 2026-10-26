"""HTTP-only compatibility client. No server imports or local credential defaults."""
import json
import hashlib
import math
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.parse import quote

import httpx


class ClientError(Exception):
    def __init__(self, code, message, status=None, context=None):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.context = context


class Client:
    def __init__(self, base_url='http://127.0.0.1:8088', *, timeout=15, http_client=None, transport=None):
        try:
            parts = urlsplit(base_url)
            parts.port
        except (ValueError, TypeError) as error:
            raise ClientError('INVALID_ARGUMENTS', 'Service URL is not a valid HTTP(S) origin.') from error
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ClientError('INVALID_ARGUMENTS', 'Service URL must be an HTTP(S) origin without credentials.')
        if parts.path not in ('', '/') or parts.query or parts.fragment:
            raise ClientError('INVALID_ARGUMENTS', 'Service URL must not contain an API path, query or fragment.')
        self.http = http_client or httpx.Client(base_url=base_url, timeout=timeout, trust_env=False, transport=transport)
        self.request_timeout = timeout
        self._owns_http = http_client is None
        self.http.headers['Origin'] = f'{parts.scheme}://{parts.netloc}'

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._owns_http:
            self.http.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.RequestError as error:
            raise ClientError('CONNECTION_FAILED', 'Could not reach the control service.') from error
        if not response.is_success:
            context = None
            code = 'AUTH_REQUIRED' if response.status_code in (401, 403) else 'NOT_FOUND' if response.status_code == 404 else 'HTTP_ERROR'
            message = f'Control service returned HTTP {response.status_code}.'
            # Only allow known workspace codes; never echo arbitrary response bodies.
            if path.startswith('/api/workspace/'):
                try:
                    detail = response.json().get('detail', {})
                    known = {'REVISION_CONFLICT', 'TARGET_NOT_FOUND', 'CONNECTION_NOT_FOUND', 'NODE_NOT_REGISTERED',
                             'TARGET_MISMATCH', 'IDENTITY_MISMATCH', 'INVALID_OPERATION_REQUEST', 'REQUEST_TOO_LARGE',
                             'OPERATION_NOT_FOUND', 'IDEMPOTENCY_CONFLICT', 'ACTION_NOT_IMPLEMENTED', 'OPERATION_NOT_READY',
                             'SESSION_BINDING_MISMATCH', 'PREVIOUS_RESULT_UNKNOWN', 'CANCELLATION_UNSUPPORTED',
                             'CURSOR_INVALID', 'CURSOR_EXPIRED', 'STREAM_NOT_FOUND', 'STREAM_PORT_UNAVAILABLE',
                             'LEASE_REVISION_CONFLICT','LEASE_NOT_EXTENDED','SESSION_NOT_FOUND'}
                    known.update({'ARTIFACT_NOT_FOUND','ARTIFACT_INVALID','ARTIFACT_TOO_LARGE','ARTIFACT_INTEGRITY_FAILED','ARTIFACT_ID_CONFLICT'})
                    if isinstance(detail, dict) and detail.get('code') in known:
                        code = detail['code']
                        if code == 'CURSOR_EXPIRED' and isinstance(detail.get('context'), dict):
                            context = {key:value for key,value in detail['context'].items()
                                       if key in ('stream_id','earliest_after','latest_after') and isinstance(value,str) and len(value)<=512}
                            floor = detail['context'].get('trimmed_through')
                            if type(floor) is int and floor >= 0:
                                context['trimmed_through'] = floor
                except (ValueError, AttributeError):
                    pass
            # Do not print raw response bodies or submitted credentials.
            raise ClientError(code, message, response.status_code, context)
        try:
            return json.loads(response.content, parse_constant=lambda value: self._invalid_constant())
        except ValueError as error:
            raise ClientError('INVALID_RESPONSE', 'Control service did not return valid JSON.', response.status_code) from error

    @staticmethod
    def _invalid_constant():
        raise ValueError('Non-finite JSON number')

    def login(self, username, password):
        return self.post('/api/login', {'username': username, 'password': password})

    def get(self, path):
        return self._request('GET', path)

    def post(self, path, body):
        return self._request('POST', path, json=body)

    def upload(self, image):
        return self._request('POST', '/api/firmware', content=Path(image).read_bytes(), headers={'Content-Type':'application/octet-stream'})

    def artifacts(self, *, before=None, limit=50):
        return self._request('GET','/api/workspace/artifacts',params={k:v for k,v in {'before':before,'limit':limit}.items() if v is not None})

    def artifact(self, identity):
        return self.get('/api/workspace/artifacts/'+quote(identity,safe=''))

    def import_artifact(self, image):
        try:
            with Path(image).open('rb') as stream:
                data=stream.read(0x300001)
        except OSError as error:
            raise ClientError('INVALID_ARGUMENTS','Could not read the node firmware file.') from error
        if not 256<=len(data)<=0x300000:
            raise ClientError('INVALID_ARGUMENTS','Node firmware must be between 256 bytes and 3 MiB.')
        digest=hashlib.sha256(data).hexdigest()
        result=self._request('POST','/api/workspace/artifacts',content=data,headers={'Content-Type':'application/octet-stream'})
        if not isinstance(result,dict) or result.get('sha256')!=digest or result.get('size')!=len(data) or result.get('id')!=digest[:24]:
            raise ClientError('INVALID_RESPONSE','Artifact receipt differs from the uploaded file identity.')
        return result

    def health(self):
        return self.get('/health')

    def nodes(self):
        rows = self.get('/api/devices')
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) or not isinstance(row.get('telemetry', {}), dict) for row in rows):
            raise ClientError('INVALID_RESPONSE', 'Invalid device inventory response.')
        return rows

    def node(self, node_id):
        for row in self.nodes():
            if row['id'] == node_id:
                return row
        raise ClientError('NOT_FOUND', 'Node was not found.', 404)

    def profiles(self):
        result = self.get('/api/debug')
        if not isinstance(result, dict) or not isinstance(result.get('profiles', []), list):
            raise ClientError('INVALID_RESPONSE', 'Invalid interface profile response.')
        profiles = result.get('profiles', [])
        if any(not isinstance(p, dict) or not isinstance(p.get('members', []), list) or any(not isinstance(m, dict) or not isinstance(m.get('device_id'), str) for m in p.get('members', [])) for p in profiles):
            raise ClientError('INVALID_RESPONSE', 'Invalid interface profile members.')
        return profiles

    def capabilities(self, node_id):
        node = self.node(node_id)
        return {'node_id': node_id, 'boot_id': node.get('boot_id'), 'online': node.get('online'),
                'firmware_version': node.get('version'),
                'runtime':node.get('telemetry',{}).get('runtime'),
                'reported': node.get('telemetry', {}).get('capabilities', []),
                'profiles': [p for p in self.profiles() if any(m['device_id'] == node_id for m in p.get('members', []))]}

    def configurations(self, kind):
        if kind not in ('target', 'connection'):
            raise ClientError('INVALID_ARGUMENTS', 'Configuration kind must be target or connection.')
        result = self.get(f'/api/workspace/{kind}s')
        if not isinstance(result, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in result):
            raise ClientError('INVALID_RESPONSE', 'Invalid configuration inventory response.')
        return result

    def configuration(self, kind, identity, revision=None):
        if kind not in ('target', 'connection'):
            raise ClientError('INVALID_ARGUMENTS', 'Configuration kind must be target or connection.')
        path = f'/api/workspace/{kind}s/{quote(identity, safe="")}'
        return self._request('GET', path, params={'revision': revision} if revision is not None else {})

    def apply_configuration(self, kind, configuration, expected_revision):
        if kind not in ('target', 'connection') or not isinstance(configuration, dict) or not isinstance(configuration.get('id'), str):
            raise ClientError('INVALID_ARGUMENTS', 'Configuration requires a target/connection kind and identity.')
        path = f'/api/workspace/{kind}s/{quote(configuration["id"], safe="")}'
        return self._request('PUT', path, json={'configuration': configuration, 'expected_revision': expected_revision})

    def plan(self, request):
        result = self.post('/api/workspace/operation-plan', request)
        if not isinstance(result, dict) or result.get('status') not in ('supported', 'unsupported', 'blocked', 'unknown') or result.get('reserved') is not False:
            raise ClientError('INVALID_RESPONSE', 'Invalid operation plan response.')
        return result

    def submit_operation(self, request):
        return self.post('/api/workspace/operations', request)

    def operation(self, identity):
        return self.get('/api/workspace/operations/' + quote(identity, safe=''))

    def operations(self, *, node=None, before=None, limit=50):
        return self._request('GET', '/api/workspace/operations', params={k:v for k,v in
            {'node':node, 'before':before, 'limit':limit}.items() if v is not None})

    def operation_events(self, identity, *, after=0, limit=100):
        return self._request('GET', '/api/workspace/operations/' + quote(identity, safe='') + '/events', params={'after':after, 'limit':limit})

    def cancel_operation(self, identity):
        return self.post('/api/workspace/operations/' + quote(identity, safe='') + '/cancel', {})

    def session(self, identity):
        return self.get('/api/workspace/sessions/' + quote(identity,safe=''))

    def sessions(self, *, node=None, before=None, limit=50):
        return self._request('GET','/api/workspace/sessions',params={key:value for key,value in
            {'node':node,'before':before,'limit':limit}.items() if value is not None})

    def wait_operation(self, identity, *, timeout=30, interval=0.5, initial=None):
        if timeout <= 0 or not math.isfinite(timeout) or interval <= 0 or not math.isfinite(interval):
            raise ClientError('INVALID_ARGUMENTS', 'Wait timeout must be finite and positive.')
        deadline = time.monotonic() + timeout
        result = initial or {'id': identity}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**result, 'wait_timed_out': True}
            try:
                result = self._request('GET', '/api/workspace/operations/' + quote(identity, safe=''), timeout=min(remaining, self.request_timeout))
            except ClientError as error:
                return {**result, 'wait_timed_out': time.monotonic() >= deadline,
                        'wait_error': {'code': error.code, 'message': str(error)}}
            if not isinstance(result, dict) or result.get('state') not in ('accepted','dispatched','releasing','finished'):
                return {'id': identity, 'wait_timed_out': False, 'wait_error':
                        {'code': 'INVALID_RESPONSE', 'message': 'Invalid operation state response.'}}
            if result['state'] == 'finished':
                return {**result, 'wait_timed_out': False}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**result, 'wait_timed_out': True}
            time.sleep(min(interval, remaining))

    def streams(self, *, node=None, session=None, before=None, limit=50):
        return self._request('GET', '/api/workspace/streams', params={key:value for key,value in
            {'node':node,'session':session,'before':before,'limit':limit}.items() if value is not None})

    def stream(self, identity):
        return self.get('/api/workspace/streams/' + quote(identity, safe=''))

    def read_stream(self, identity, *, after=None, limit=100, limit_bytes=4096, port=None, request_timeout=None):
        params = {key:value for key,value in {'after':after,'limit':limit,'limit_bytes':limit_bytes,'port':port}.items() if value is not None}
        extra = {'timeout': request_timeout} if request_timeout is not None else {}
        result = self._request('GET', '/api/workspace/streams/' + quote(identity, safe='') + '/read', params=params, **extra)
        if not isinstance(result, dict) or result.get('api_version') != 'edbg.stream.read/v0' or not isinstance(result.get('items'),list) or not isinstance(result.get('next_after'),str):
            raise ClientError('INVALID_RESPONSE', 'Invalid receive stream page.')
        return result

    def follow_stream(self, identity, *, after=None, duration=30, interval=0.5, limit=100, limit_bytes=4096, port=None):
        if not math.isfinite(duration) or duration<=0 or not math.isfinite(interval) or interval<=0:
            raise ClientError('INVALID_ARGUMENTS', 'Follow duration and interval must be finite and positive.')
        deadline = time.monotonic() + duration
        cursor, first = after, True
        while time.monotonic() < deadline:
            try:
                page = self.read_stream(identity, after=cursor, limit=limit, limit_bytes=limit_bytes, port=port,
                                        request_timeout=min(self.request_timeout, max(.001, deadline-time.monotonic())))
            except ClientError as error:
                error.context = {**(error.context or {}), 'resume_after': cursor}
                raise
            changed = page['next_after'] != cursor
            cursor = page['next_after']
            if first or changed or page['items'] or page.get('eof'):
                yield page
            first = False
            if page.get('eof'):
                return
            if not page.get('has_more'):
                remaining = deadline-time.monotonic()
                if remaining>0:
                    time.sleep(min(interval,remaining))

    def jobs(self, node_id=None):
        rows = self.get('/api/jobs')
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('device_id'), str) for row in rows):
            raise ClientError('INVALID_RESPONSE', 'Invalid job response.')
        return [row for row in rows if not node_id or row['device_id'] == node_id]

    def job(self, device_id, kind, firmware_id=None):
        if kind not in ('diagnostics','restart') or firmware_id is not None:
            raise ClientError('INVALID_ARGUMENTS','Use a node.update operation and a registered image for OTA.')
        return self.post('/api/jobs', {'device_ids': [device_id], 'kind': kind})['job_ids'][0]

    def wait(self, job_id, timeout=150, on_progress=None):
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            job = next((j for j in self.jobs() if j['id'] == job_id), None)
            if job is None:
                raise ClientError('NOT_FOUND', 'Job was not found.', 404)
            state = (job['state'], job['progress'])
            if state != last:
                if on_progress:
                    on_progress(job)
                last = state
            if job['state'] in ('succeeded', 'failed', 'cancelled'):
                return job
            time.sleep(2)
        raise TimeoutError(f'Wait expired for job {job_id}; the job was not cancelled.')

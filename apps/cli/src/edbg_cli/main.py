"""Workspace commands and bounded stream readers over the public HTTP SDK."""
import argparse
import json
import math
import os
import sys
from pathlib import Path

from edbg_sdk import Client, ClientError

SCHEMA = 'edbg.cli.query/v0'


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='Emit a structured query result.')
    common.add_argument('--url', default=argparse.SUPPRESS, help='Control service origin; defaults to EDBG_URL or localhost:8088.')
    common.add_argument('--credentials-file', default=argparse.SUPPRESS, help='Explicit local JSON file containing username and password.')
    common.add_argument('--timeout', type=float, default=argparse.SUPPRESS, help='HTTP timeout in seconds.')
    root = Parser(prog='edbg', description='Embedded workspace configuration, plans and compatibility operations.', parents=[common])
    commands = root.add_subparsers(dest='command', required=True)
    commands.add_parser('health', parents=[common], help='Query control service health.')
    for name in ('node', 'capability', 'profile', 'job'):
        group = commands.add_parser(name, parents=[common])
        verbs = group.add_subparsers(dest='verb', required=True)
        listing = verbs.add_parser('list', parents=[common])
        if name == 'capability':
            listing.add_argument('--node', required=True)
        elif name == 'job':
            listing.add_argument('--node')
        elif name == 'node':
            show = verbs.add_parser('show', parents=[common])
            show.add_argument('node_id')
    for name in ('target', 'connection'):
        group = commands.add_parser(name, parents=[common])
        verbs = group.add_subparsers(dest='verb', required=True)
        verbs.add_parser('list', parents=[common])
        show = verbs.add_parser('show', parents=[common])
        show.add_argument('identity')
        show.add_argument('--revision', type=int)
        apply = verbs.add_parser('apply', parents=[common])
        apply.add_argument('--file', required=True)
        apply.add_argument('--expect-revision', type=int, required=True)
    operations = commands.add_parser('operation', parents=[common]).add_subparsers(dest='verb', required=True)
    plan = operations.add_parser('plan', parents=[common], help='Assess a draft request without dispatch or reservation.')
    plan.add_argument('--file', required=True, help='JSON request path, or - for standard input.')
    submit = operations.add_parser('submit', parents=[common], help='Submit an explicitly keyed compatible hardware intent.')
    submit.add_argument('--file', required=True)
    submit.add_argument('--wait', action='store_true')
    submit.add_argument('--wait-timeout', type=float, default=30)
    for verb in ('get', 'cancel', 'events'):
        item = operations.add_parser(verb, parents=[common])
        item.add_argument('identity')
        if verb == 'events':
            item.add_argument('--after', type=int, default=0)
            item.add_argument('--limit', type=int, default=100)
    listing = operations.add_parser('list', parents=[common])
    listing.add_argument('--node')
    listing.add_argument('--before')
    listing.add_argument('--limit', type=int, default=50)
    artifact=commands.add_parser('artifact',parents=[common]).add_subparsers(dest='verb',required=True)
    listing=artifact.add_parser('list',parents=[common]);listing.add_argument('--before');listing.add_argument('--limit',type=int,default=50)
    show=artifact.add_parser('show',parents=[common]);show.add_argument('identity')
    importing=artifact.add_parser('import',parents=[common],help='Register immutable node firmware bytes; does not dispatch OTA.')
    importing.add_argument('--image',required=True)
    session=commands.add_parser('session',parents=[common]).add_subparsers(dest='verb',required=True)
    show=session.add_parser('show',parents=[common]);show.add_argument('identity')
    listing=session.add_parser('list',parents=[common]);listing.add_argument('--node');listing.add_argument('--before')
    listing.add_argument('--limit',type=int,default=50)
    stream = commands.add_parser('stream', parents=[common]).add_subparsers(dest='verb', required=True)
    listing = stream.add_parser('list', parents=[common])
    listing.add_argument('--node'); listing.add_argument('--session'); listing.add_argument('--before')
    listing.add_argument('--limit', type=int, default=50)
    show = stream.add_parser('show', parents=[common]); show.add_argument('identity')
    for verb in ('read','follow'):
        item = stream.add_parser(verb, parents=[common]); item.add_argument('identity')
        item.add_argument('--after', help='Explicit cursor; expiry is reported without automatic skipping.')
        item.add_argument('--limit', type=int, default=100)
        item.add_argument('--limit-bytes', type=int, default=4096)
        item.add_argument('--port', type=int, choices=(1,2))
        if verb == 'follow':
            item.add_argument('--duration', type=float, default=30, help='Finite follow duration in seconds; default 30.')
            item.add_argument('--interval', type=float, default=.5)
            item.add_argument('--format', choices=('ndjson',), default='ndjson')
    return root


def error_exit(code):
    return {'AUTH_REQUIRED':3,'CONNECTION_FAILED':4,'INVALID_RESPONSE':6,'CURSOR_EXPIRED':10,
            'CURSOR_INVALID':2,'STREAM_PORT_UNAVAILABLE':2,'STREAM_NOT_FOUND':5}.get(code,5)


def follow(args, client):
    cursor = args.after
    ended = False
    try:
        for page in client.follow_stream(args.identity, after=args.after, duration=args.duration, interval=args.interval,
                                       limit=args.limit, limit_bytes=args.limit_bytes, port=args.port):
            cursor = page['next_after']; ended = bool(page.get('eof'))
            print(json.dumps({'api_version':'edbg.stream.follow/v0','kind':'page','data':page},ensure_ascii=False), flush=True)
        print(json.dumps({'api_version':'edbg.stream.follow/v0','kind':'checkpoint','stream_id':args.identity,
                          'resume_after':cursor,'reason':'source_ended' if ended else 'duration'},ensure_ascii=False),flush=True)
        return 0
    except ClientError as error:
        detail = {'code':error.code,'message':str(error)}
        if error.context:
            detail['context'] = error.context
        print(json.dumps({'api_version':'edbg.stream.follow/v0','kind':'error','stream_id':args.identity,
                          'resume_after':cursor,'error':detail},ensure_ascii=False),flush=True)
        return error_exit(error.code)
    except KeyboardInterrupt:
        print(json.dumps({'api_version':'edbg.stream.follow/v0','kind':'checkpoint','stream_id':args.identity,
                          'resume_after':cursor,'reason':'interrupted'}),flush=True)
        return 130


def unique_pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError('Non-finite number')


def document(path):
    try:
        if path == '-':
            raw = sys.stdin.read(262145)
        else:
            with Path(path).open(encoding='utf-8-sig') as stream:
                raw = stream.read(262145)
        if len(raw.encode('utf-8')) > 262144:
            raise ValueError('Oversize JSON document')
        return json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=invalid_constant)
    except (OSError, ValueError, RecursionError) as error:
        raise ClientError('INVALID_CONFIG', 'Input must be unique-key JSON within 256 KiB.') from error


def credentials(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict) or not all(isinstance(data.get(k), str) and data[k] for k in ('username','password')):
            raise ValueError('invalid credential fields')
        return {k:data[k] for k in ('username','password')}
    except (OSError, ValueError, TypeError) as error:
        raise ClientError('INVALID_CONFIG', 'Credentials file must contain username and password strings.') from error


def node_view(node):
    # Inventory results deliberately exclude tokens and internal storage fields.
    return {**{k:node.get(k) for k in ('id','name','group_name','online','last_seen','boot_id','version')},
            'capabilities':node.get('telemetry',{}).get('capabilities',[]),
            'runtime':node.get('telemetry',{}).get('runtime')}


def execute(args, client):
    if args.command == 'health':
        return client.health()
    if args.command == 'node':
        return [node_view(n) for n in client.nodes()] if args.verb == 'list' else node_view(client.node(args.node_id))
    if args.command == 'capability':
        return client.capabilities(args.node)
    if args.command == 'profile':
        return client.profiles()
    if args.command == 'job':
        return client.jobs(getattr(args, 'node', None))
    if args.command in ('target', 'connection'):
        if args.verb == 'list':
            return client.configurations(args.command)
        if args.verb == 'show':
            return client.configuration(args.command, args.identity, args.revision)
        return client.apply_configuration(args.command, args.input_document, args.expect_revision)
    if args.command == 'operation':
        if args.verb == 'plan':
            return client.plan(args.input_document)
        if args.verb == 'submit':
            result = client.submit_operation(args.input_document)
            return client.wait_operation(result['id'], timeout=args.wait_timeout, initial=result) if args.wait else result
        if args.verb == 'get':
            return client.operation(args.identity)
        if args.verb == 'cancel':
            return client.cancel_operation(args.identity)
        if args.verb == 'events':
            return client.operation_events(args.identity, after=args.after, limit=args.limit)
        return client.operations(node=args.node, before=args.before, limit=args.limit)
    if args.command == 'stream':
        if args.verb == 'list':
            return client.streams(node=args.node,session=args.session,before=args.before,limit=args.limit)
        if args.verb == 'show':
            return client.stream(args.identity)
        return client.read_stream(args.identity,after=args.after,limit=args.limit,limit_bytes=args.limit_bytes,port=args.port)
    if args.command=='session':
        return client.session(args.identity) if args.verb=='show' else client.sessions(node=args.node,before=args.before,limit=args.limit)
    if args.command=='artifact':
        if args.verb=='list':return client.artifacts(before=args.before,limit=args.limit)
        if args.verb=='show':return client.artifact(args.identity)
        return client.import_artifact(args.image)
    raise UsageError('Unknown command')


def main(argv=None, *, client_factory=Client):
    argv = list(sys.argv[1:] if argv is None else argv)
    machine = '--json' in argv
    is_follow = False
    try:
        args = parser().parse_args(argv)
        is_follow = args.command == 'stream' and args.verb == 'follow'
        if hasattr(args, 'expect_revision') and args.expect_revision < 0:
            raise UsageError('Expected revision must be zero for creation or positive for updates.')
        if getattr(args, 'revision', None) is not None and args.revision < 1:
            raise UsageError('Historical revision must be positive.')
        if hasattr(args, 'file'):
            args.input_document = document(args.file)
        if hasattr(args, 'wait_timeout') and (not math.isfinite(args.wait_timeout) or args.wait_timeout <= 0):
            raise UsageError('Wait timeout must be finite and positive.')
        if isinstance(getattr(args,'after',None), int) and args.after < 0 or hasattr(args, 'limit') and not 1 <= args.limit <= 200:
            raise UsageError('Cursor and result limit are outside their allowed range.')
        if hasattr(args,'limit_bytes') and not 128 <= args.limit_bytes <= 65536:
            raise UsageError('Stream byte limit must be between 128 and 65536.')
        if hasattr(args,'duration') and (not math.isfinite(args.duration) or args.duration<=0 or args.duration>86400 or not math.isfinite(args.interval) or args.interval<=0):
            raise UsageError('Follow duration must be in (0,86400] seconds and interval must be positive.')
        timeout = getattr(args, 'timeout', 15)
        if not math.isfinite(timeout) or timeout <= 0:
            raise UsageError('timeout must be a finite number greater than zero')
        url = getattr(args, 'url', os.environ.get('EDBG_URL', 'http://127.0.0.1:8088'))
        auth = getattr(args, 'credentials_file', os.environ.get('EDBG_CREDENTIALS_FILE'))
        with client_factory(url, timeout=timeout) as client:
            if auth:
                client.login(**credentials(auth))
            if args.command == 'stream' and args.verb == 'follow':
                return follow(args,client)
            result = execute(args, client)
        output = {'schema':SCHEMA,'ok':True,'data':result}
        if machine:
            print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.command == 'operation' and args.verb == 'submit' and args.wait:
            if result.get('wait_error') and not result.get('wait_timed_out'):
                return {'AUTH_REQUIRED':3, 'INVALID_RESPONSE':6, 'CONNECTION_FAILED':4}.get(result['wait_error']['code'],5)
            return 8 if result.get('wait_timed_out') else 0 if result.get('outcome') == 'succeeded' else 9
        return 0
    except (UsageError, ClientError) as error:
        code = 'INVALID_ARGUMENTS' if isinstance(error, UsageError) else error.code
        detail = {'code':code,'message':str(error)}
        if isinstance(error, ClientError) and error.status is not None:
            detail['http_status'] = error.status
        if isinstance(error, ClientError) and error.context:
            detail['context'] = error.context
        if is_follow:
            print(json.dumps({'api_version':'edbg.stream.follow/v0','kind':'error','stream_id':args.identity,
                              'resume_after':args.after,'error':detail},ensure_ascii=False),flush=True)
        elif machine:
            print(json.dumps({'schema':SCHEMA,'ok':False,'error':detail}, ensure_ascii=False))
        else:
            print(f'{code}: {error}', file=sys.stderr)
        return {'INVALID_ARGUMENTS':2,'INVALID_CONFIG':2,'AUTH_REQUIRED':3,'CONNECTION_FAILED':4,'NOT_FOUND':5,'HTTP_ERROR':5,'INVALID_RESPONSE':6,
                'REVISION_CONFLICT':7,'TARGET_NOT_FOUND':5,'CONNECTION_NOT_FOUND':5,'NODE_NOT_REGISTERED':2,
                'TARGET_MISMATCH':2,'IDENTITY_MISMATCH':2,'INVALID_OPERATION_REQUEST':2,'REQUEST_TOO_LARGE':2,
                'IDEMPOTENCY_CONFLICT':7,'OPERATION_NOT_FOUND':5,'ACTION_NOT_IMPLEMENTED':2,'OPERATION_NOT_READY':7,
                'SESSION_BINDING_MISMATCH':7,'PREVIOUS_RESULT_UNKNOWN':7,'CANCELLATION_UNSUPPORTED':2,
                'CURSOR_EXPIRED':10,'CURSOR_INVALID':2,'STREAM_NOT_FOUND':5,'STREAM_PORT_UNAVAILABLE':2,
                'LEASE_REVISION_CONFLICT':7,'LEASE_NOT_EXTENDED':7,'SESSION_NOT_FOUND':5}.get(code, 1)

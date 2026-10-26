import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from edbg_sdk import Client, ClientError
from edbg_cli.main import main


class MachineClientTests(unittest.TestCase):
    def test_artifact_import_validates_receipt_and_uses_bounded_binary_input(self):
        import hashlib
        data=b'e'*256;digest=hashlib.sha256(data).hexdigest()
        requests=[]
        def handler(request):
            requests.append(request);self.assertEqual(request.content,data)
            self.assertEqual(request.method,'POST');self.assertEqual(request.url.path,'/api/workspace/artifacts')
            return httpx.Response(200,json={'id':digest[:24],'sha256':'0'*64,'size':len(data)})
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'fixture.bin';image.write_bytes(data)
            code,result,_=self.invoke(['artifact','import','--image',str(image),'--json'],handler)
            self.assertEqual(code,6);self.assertEqual(result['error']['code'],'INVALID_RESPONSE');self.assertEqual(len(requests),1)
            image.write_bytes(b'e'*0x300001)
            code,result,_=self.invoke(['artifact','import','--image',str(image),'--json'],handler)
            self.assertEqual(code,2);self.assertEqual(len(requests),1)

    def invoke(self, argv, handler):
        stdout, stderr = io.StringIO(), io.StringIO()
        factory = lambda url, **kw: Client(url, transport=httpx.MockTransport(handler), **kw)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv, client_factory=factory)
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_explicit_auth_and_node_inventory_use_http_and_exclude_internal_fields(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers['origin'], 'http://testserver')
            if request.url.path == '/api/login':
                self.assertEqual(json.loads(request.content), {'username':'operator','password':'private-value'})
                return httpx.Response(200, json={'ok':True}, headers={'set-cookie':'fleet_session=example; Path=/; HttpOnly'})
            self.assertIn('fleet_session=example', request.headers['cookie'])
            return httpx.Response(200, json=[{'id':'node-one','name':'Probe','online':True,'telemetry':{'capabilities':['uart']},'token_hash':'must-not-be-output'}])
        with tempfile.TemporaryDirectory() as directory:
            auth = Path(directory)/'credentials.json'
            auth.write_text(json.dumps({'username':'operator','password':'private-value'}), encoding='utf-8')
            code, result, errors = self.invoke(['node','list','--json','--url','http://testserver','--credentials-file',str(auth)], handler)
        self.assertEqual(code,0);self.assertTrue(result['ok']);self.assertEqual(result['schema'],'edbg.cli.query/v0')
        self.assertEqual(result['data'][0]['capabilities'],['uart'])
        self.assertNotIn('token_hash',result['data'][0]);self.assertNotIn('private-value',json.dumps(result));self.assertEqual(errors,'')
        self.assertEqual([r.url.path for r in requests],['/api/login','/api/devices'])

    def test_json_flag_before_command_and_service_health_require_no_login(self):
        def handler(request):
            self.assertEqual(request.method,'GET');self.assertEqual(request.url.path,'/health')
            return httpx.Response(200,json={'ok':True})
        code,result,_=self.invoke(['--json','health'],handler)
        self.assertEqual(code,0);self.assertEqual(result['data'],{'ok':True})

    def test_auth_network_and_invalid_payload_errors_are_machine_readable(self):
        cases=[(lambda r:httpx.Response(401,json={'detail':'do-not-print-response-secret'}),3,'AUTH_REQUIRED'),
               (lambda r:httpx.Response(200,text='<html>not JSON</html>'),6,'INVALID_RESPONSE'),
               (lambda r:httpx.Response(200,text='[{"id":"one","telemetry":{"value":NaN}}]'),6,'INVALID_RESPONSE'),
               (lambda r:httpx.Response(200,json={'unexpected':'inventory'}),6,'INVALID_RESPONSE')]
        for handler,expected,code_name in cases:
            code,result,errors=self.invoke(['node','list','--json'],handler)
            self.assertEqual(code,expected);self.assertFalse(result['ok']);self.assertEqual(result['error']['code'],code_name)
            self.assertNotIn('response-secret',json.dumps(result));self.assertEqual(errors,'')
        def disconnected(request):raise httpx.ConnectError('private transport details',request=request)
        code,result,_=self.invoke(['health','--json'],disconnected)
        self.assertEqual(code,4);self.assertEqual(result['error']['code'],'CONNECTION_FAILED')
        self.assertNotIn('transport details',json.dumps(result))
        code,result,_=self.invoke(['profile','list','--json'],lambda r:httpx.Response(200,json={'profiles':[{'members':['invalid-member']}]}))
        self.assertEqual(code,6);self.assertEqual(result['error']['code'],'INVALID_RESPONSE')

    def test_invalid_arguments_and_nonfinite_timeout_never_call_service(self):
        def unused(request):self.fail('Invalid arguments contacted the service')
        for argv in [['--json','unknown'],['node','list','--json','--timeout','nan'],['health','--json','--timeout','0']]:
            code,result,_=self.invoke(argv,unused)
            self.assertEqual(code,2);self.assertEqual(result['error']['code'],'INVALID_ARGUMENTS')

    def test_capability_discovery_is_scoped_to_requested_node(self):
        def handler(request):
            if request.url.path=='/api/devices':return httpx.Response(200,json=[{'id':'one','online':False,'boot_id':'boot','version':'3.5.0','telemetry':{'capabilities':['i2c_master_v2']}}])
            return httpx.Response(200,json={'profiles':[{'id':'matching','members':[{'device_id':'one'}]},{'id':'other','members':[{'device_id':'two'}]}]})
        code,result,_=self.invoke(['capability','list','--node','one','--json'],handler)
        self.assertEqual(code,0);self.assertEqual(result['data']['reported'],['i2c_master_v2'])
        self.assertEqual([p['id'] for p in result['data']['profiles']],['matching'])
        self.assertFalse(result['data']['online'])

    def test_sdk_rejects_credentials_embedded_in_service_url(self):
        with self.assertRaises(ClientError) as result:Client('http://operator:secret@example.invalid')
        self.assertEqual(result.exception.code,'INVALID_ARGUMENTS');self.assertNotIn('secret',str(result.exception))

    def test_wait_connection_failure_retains_the_accepted_operation_identity(self):
        def disconnected(request):raise httpx.ConnectError('private transport details',request=request)
        with Client('http://testserver',transport=httpx.MockTransport(disconnected)) as client:
            result=client.wait_operation('accepted-operation',initial={'id':'accepted-operation','state':'accepted'})
        self.assertEqual(result['id'],'accepted-operation')
        self.assertEqual(result['wait_error']['code'],'CONNECTION_FAILED')
        self.assertNotIn('transport details',json.dumps(result))

    def test_follow_disconnect_preserves_the_last_delivered_cursor(self):
        calls=[]
        def handler(request):
            calls.append(request)
            if len(calls)==1:
                return httpx.Response(200,json={'api_version':'edbg.stream.read/v0','items':[],
                    'next_after':'c1.delivered-checkpoint','has_more':False,'eof':False})
            raise httpx.ConnectError('private transport detail',request=request)
        with Client('http://testserver',transport=httpx.MockTransport(handler)) as client:
            follower=client.follow_stream('test-stream',duration=1,interval=.001)
            self.assertEqual(next(follower)['next_after'],'c1.delivered-checkpoint')
            with self.assertRaises(ClientError) as error:next(follower)
        self.assertEqual(error.exception.context['resume_after'],'c1.delivered-checkpoint')
        self.assertNotIn('private transport detail',str(error.exception))


if __name__=='__main__':unittest.main()

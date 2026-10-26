"""Enroll one device and generate its private NVS image. Does not flash hardware."""
import argparse
import csv
import json
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from pathlib import Path
import httpx
from cryptography import x509

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--id', required=True, help='s3- plus Wi-Fi STA MAC without colons')
parser.add_argument('--name', required=True)
parser.add_argument('--group', default='实验室')
parser.add_argument('--server', help='HTTPS URL reachable by ESP32')
parser.add_argument('--ca', type=Path, help='Trusted root CA PEM for the server (defaults to local development CA)')
parser.add_argument('--public-ca-bundle', action='store_true', help='Use built-in public CA roots for a public HTTPS domain (firmware >=3.1.1)')
parser.add_argument('--wifi-config', type=Path, default=ROOT.parent / 'simple_ota' / 'config.h')
parser.add_argument('--token-file', type=Path, help='Use saved one-time enrollment response instead of enrolling again')
args = parser.parse_args()
if not re.fullmatch(r's3-[0-9a-f]{12}', args.id):
    parser.error('Invalid device ID')
state = ROOT / '.state'
state.mkdir(parents=True, exist_ok=True)
url = args.server or json.loads((state / 'local.json').read_text())['device_url']
if not url.startswith('https://'):
    parser.error('HTTPS is required')
if args.ca and args.public_ca_bundle:
    parser.error('Choose --ca OR --public-ca-bundle')
parsed = urlsplit(url)
if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/') or len(url) > 185:
    parser.error('Use an HTTPS origin, without username, password, query or path prefix')
ca_path = args.ca.resolve() if args.ca else state / 'ca.pem'
if not args.public_ca_bundle:
    ca_bytes = ca_path.read_bytes()
    if len(ca_bytes) >= 4096 or b'PRIVATE KEY' in ca_bytes:
        parser.error('Provide only a root CA certificate PEM smaller than 4096 bytes')
    certificate = x509.load_pem_x509_certificate(ca_bytes)
    if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        parser.error('The CA certificate must have CA=true')
config = args.wifi_config.read_text(encoding='utf-8-sig')


def setting(name):
    match = re.search(r'^#define\s+' + name + r'\s+("(?:\\.|[^"\\])*")', config, re.M)
    if not match:
        raise RuntimeError('Missing ' + name)
    return json.loads(match.group(1))


enrollment_path = state / (args.id + '.enrollment.local.json')
if args.token_file:
    enrolled = json.loads(args.token_file.read_text())
elif enrollment_path.exists():
    enrolled = json.loads(enrollment_path.read_text())
else:
    admin = json.loads((state / 'admin.local.json').read_text())
    with httpx.Client(base_url='http://127.0.0.1:8088', headers={'Origin': 'http://127.0.0.1:8088'}, trust_env=False) as client:
        client.post('/api/login', json=admin).raise_for_status()
        response = client.post('/api/devices', json={'id': args.id, 'name': args.name, 'group': args.group})
        response.raise_for_status()
        enrolled = response.json()
    enrollment_path.write_text(json.dumps(enrolled), encoding='utf-8')
if enrolled.get('id') != args.id or not 20 <= len(enrolled.get('token', '')) <= 79:
    parser.error('Enrollment identity/token does not match the intended device')
ssid, password = setting('WIFI_SSID'), setting('WIFI_PASSWORD')
if not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(password.encode()) <= 64:
    parser.error('Expected a 1-32 byte SSID and 8-64 byte WPA2 password')
csv_path = state / (args.id + '.provision.local.csv')
binary_path = state / (args.id + '.nvs.local.bin')
with csv_path.open('w', encoding='utf-8', newline='') as stream:
    writer = csv.writer(stream)
    writer.writerows([
        ['key', 'type', 'encoding', 'value'],
        ['fleet', 'namespace', '', ''],
        ['ssid', 'data', 'string', ssid],
        ['password', 'data', 'string', password],
        ['server', 'data', 'string', url.rstrip('/')],
        ['token', 'data', 'string', enrolled['token']],
        ['ca', 'data', 'string', 'SYSTEM'] if args.public_ca_bundle else ['ca', 'file', 'string', str(ca_path)],
        ['epoch', 'data', 'i64', int(time.time())],
    ])
idf = Path.home() / 'esp/v5.3.1/esp-idf'
python = Path.home() / '.espressif/python_env/idf5.3_py3.11_env/Scripts/python.exe'
tool = idf / 'components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py'
result = subprocess.run([str(python), str(tool), 'generate', str(csv_path), str(binary_path), '0x5000'], capture_output=True)
csv_path.unlink(missing_ok=True)
if result.returncode:
    raise SystemExit('NVS generation failed; credentials were not printed. Check local configuration.')
print('Device enrolled:', args.id, args.name)
print('Private NVS image:', binary_path)
print('Wi-Fi credentials and device token were not embedded in the shared firmware.')

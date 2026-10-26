"""Create a local TLS CA and server certificate; never install trust into Windows."""
import argparse
import ipaddress
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--address', required=True, help='PC LAN IPv4 address')
args = parser.parse_args()
state = ROOT / '.state'
state.mkdir(exist_ok=True)
ca_key_path = state / 'ca.key'
now = datetime.now(timezone.utc)


def private_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


if ca_key_path.exists():
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    ca = x509.load_pem_x509_certificate((state / 'ca.pem').read_bytes())
else:
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ESP32 Fleet Development CA')])
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    ca_key_path.write_bytes(private_bytes(ca_key))
    (state / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.address)])
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(ca.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(args.address)), x509.IPAddress(ipaddress.ip_address('127.0.0.1')), x509.DNSName('localhost')]), critical=False)
        .sign(ca_key, hashes.SHA256()))
(state / 'server.key').write_bytes(private_bytes(key))
(state / 'server.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
admin = state / 'admin.local.json'
if not admin.exists():
    admin.write_text(json.dumps({'username': 'admin', 'password': secrets.token_urlsafe(18)}), encoding='utf-8')
(state / 'local.json').write_text(json.dumps({'device_url': f'https://{args.address}:8443', 'console_url': 'http://127.0.0.1:8088'}), encoding='utf-8')
print('Console: http://127.0.0.1:8088')
print(f'Device service: https://{args.address}:8443')
print('Credentials: fleet/.state/admin.local.json; CA: fleet/.state/ca.pem')

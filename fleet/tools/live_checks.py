"""Run diagnostics, reboot and an HTTPS OTA on the named real bench device."""
import argparse
import json
import time
from client import FleetClient, ROOT

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--id', default='s3-fc012cd58460')
parser.add_argument('--version', default='3.1.0')
parser.add_argument('--expect-rollback', action='store_true')
args = parser.parse_args()
client = FleetClient()
before = next(d for d in client.get('/api/devices') if d['id'] == args.id)
if not before['online']:
    raise SystemExit('Bench device is offline; no task was issued')
results = {'before': before, 'jobs': []}
if not args.expect_rollback:
    for kind in ('diagnostics', 'restart'):
        job = client.wait(client.job(args.id, kind))
        assert job['state'] == 'succeeded', job
        results['jobs'].append(job)
release = client.upload(ROOT / 'artifacts' / args.version / 'fleet_agent.bin')
job = client.wait(client.job(args.id, 'ota', release['id']))
results['jobs'].append(job)
after = next(d for d in client.get('/api/devices') if d['id'] == args.id)
if args.expect_rollback:
    assert job['state'] == 'failed' and 'rolled back' in job['detail'], job
    assert after['version'] == before['version'], (before, after)
    assert after['partition'] == before['partition'], (before, after)
    print('PASS: unconfirmed image reset and bootloader restored the previous working version.')
else:
    assert job['state'] == 'succeeded', job
    assert after['version'] == args.version and after['partition'] != before['partition'], after
    print('PASS: diagnostics, confirmed remote restart, HTTPS OTA, new-boot confirmation and partition switch.')
results['after'] = after
output = ROOT / 'artifacts' / ('rollback-check.json' if args.expect_rollback else 'live-checks.json')
output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
print('Final version:', after['version'], 'partition:', after['partition'])

"""Legacy local operator wrapper around the HTTP SDK.

Only this compatibility wrapper discovers the old local administrator file.
New clients must explicitly configure credentials.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _bootstrap import prepare_workspace
prepare_workspace()
from edbg_sdk import Client


class FleetClient(Client):
    def __init__(self):
        super().__init__('http://127.0.0.1:8088')
        try:
            self.login(**json.loads((ROOT / '.state/admin.local.json').read_text(encoding='utf-8-sig')))
        except Exception:
            self.close()
            raise

    def wait(self, job_id, timeout=150):
        def progress(job):
            print('Task:', job['kind'], job['state'], str(job['progress']) + '%', flush=True)
        return super().wait(job_id, timeout=timeout, on_progress=progress)

"""Repository-layout paths; runtime state remains compatible with the existing service."""
import os
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
LEGACY_ROOT = WORKSPACE_ROOT / 'fleet'
HARDWARE_ROOT = Path(os.environ.get('EDBG_HARDWARE_ROOT', WORKSPACE_ROOT / 'hardware')).resolve()
WEB_ROOT = Path(os.environ.get('EDBG_WEB_ROOT', WORKSPACE_ROOT / 'apps/web')).resolve()
# During batch preparation the old assets remain live until the service cutover.
if not WEB_ROOT.exists() and not os.environ.get('EDBG_WEB_ROOT'):
    WEB_ROOT = LEGACY_ROOT / 'web'

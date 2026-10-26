"""Locate source packages for legacy launchers; no state or credentials are read."""
import sys
from pathlib import Path


def prepare_workspace():
    workspace = Path(__file__).resolve().parent.parent
    for relative in ('apps/control-plane/src', 'packages/analysis/src', 'packages/sdk-python/src'):
        source = str(workspace / relative)
        if source not in sys.path:
            sys.path.insert(0, source)

"""Compatibility import. Canonical implementation lives in apps/control-plane."""
import importlib
import sys
try:
    from ._bootstrap import prepare_workspace
except ImportError:
    from _bootstrap import prepare_workspace

prepare_workspace()
sys.modules[__name__] = importlib.import_module('edbg_server.api.debug')

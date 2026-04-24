"""Pytest root conftest — makes `src/` importable as package roots.

Mirrors `monitor.py`'s `sys.path.insert(0, ...)` so tests can
`from services.seeder...` directly.
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

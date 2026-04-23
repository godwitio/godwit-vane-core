"""Stdout logger with info/debug levels.

Callable for info messages; `.debug(msg)` for verbose diagnostics that are
suppressed unless debug is enabled. Instances satisfy `Callable[[str], None]`,
so legacy call sites keep working unchanged.
"""
from datetime import datetime


class Logger:
    def __init__(self, debug_enabled: bool = False) -> None:
        self._debug_enabled = debug_enabled

    def __call__(self, msg: str) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def debug(self, msg: str) -> None:
        if self._debug_enabled:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [debug] {msg}")

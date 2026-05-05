"""Logger with sink dispatch.

A `Logger` is a `Callable[[str], None]` plus a `.debug(msg)` method.
Output destinations are pluggable as sinks — each sink is itself a
`Callable[[str], None]`. The default sink is stdout, matching prior
behaviour for any caller that constructs `Logger()` with no sinks.
"""
import glob as _glob
import os
import threading
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

Sink = Callable[[str], None]


def _stdout_sink(line: str) -> None:
    print(line)


def file_sink(path: str) -> Sink:
    f = open(path, "a", encoding="utf-8", buffering=1)  # line-buffered
    def _write(line: str) -> None:
        f.write(line + "\n")
    return _write


def render_log_path(path_template: str, day: date | None = None) -> str:
    """Render a `{date}` placeholder in a log path template to YYYY-MM-DD.

    Templates without `{date}` pass through unchanged — useful for the
    legacy `log.txt` form and for callers (TUI) that want to read the
    current day's file.
    """
    if "{date}" not in path_template:
        return path_template
    return path_template.format(date=(day or date.today()).isoformat())


def rotating_file_sink(
    path_template: str,
    retention_days: int,
    *,
    _today: Callable[[], date] = date.today,
) -> Sink:
    """Date-rotating file sink.

    `path_template` should contain a `{date}` placeholder rendered as
    YYYY-MM-DD per the local clock. The sink reopens the target file
    when the date rolls over and prunes files matching the template
    that are older than `retention_days` (counted inclusive of today,
    so `retention_days=5` keeps today + 4 prior days).

    A template with no `{date}` placeholder falls back to `file_sink` —
    no rotation, no pruning.
    """
    if "{date}" not in path_template:
        return file_sink(path_template)

    prefix, suffix = path_template.split("{date}", 1)
    glob_pattern = path_template.replace("{date}", "*")

    lock = threading.Lock()
    state: dict = {"date": None, "file": None}

    def _prune(today: date) -> None:
        cutoff = today - timedelta(days=max(retention_days, 1) - 1)
        for p in _glob.glob(glob_pattern):
            if not p.startswith(prefix) or not p.endswith(suffix):
                continue
            middle = p[len(prefix): len(p) - len(suffix)] if suffix else p[len(prefix):]
            try:
                d = date.fromisoformat(middle)
            except ValueError:
                continue
            if d < cutoff:
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _rollover(today: date) -> None:
        if state["file"] is not None:
            try:
                state["file"].close()
            except Exception:
                pass
        path = render_log_path(path_template, today)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        state["file"] = open(path, "a", encoding="utf-8", buffering=1)
        state["date"] = today
        _prune(today)

    def _write(line: str) -> None:
        with lock:
            today = _today()
            if state["date"] != today:
                _rollover(today)
            state["file"].write(line + "\n")

    return _write


def queue_sink(q) -> Sink:
    """Non-blocking sink for the TUI. Drops on full to avoid stalling
    the producing worker thread; the TUI is a courtesy view, not a
    durability requirement (the file sink is)."""
    def _put(line: str) -> None:
        try:
            q.put_nowait(line)
        except Exception:
            pass
    return _put


class Logger:
    def __init__(self, debug_enabled: bool = False,
                 sinks: Iterable[Sink] | None = None) -> None:
        self.debug_enabled = debug_enabled
        self._sinks = list(sinks) if sinks is not None else [_stdout_sink]

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        for s in self._sinks:
            s(line)

    def debug(self, msg: str) -> None:
        if not self.debug_enabled:
            return
        line = f"[{datetime.now().strftime('%H:%M:%S')}] [debug] {msg}"
        for s in self._sinks:
            s(line)

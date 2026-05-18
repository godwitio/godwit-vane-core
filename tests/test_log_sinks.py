"""Logger sink-dispatch contract.

Stdlib-only. Pins the rule that:
    - default Logger() writes to stdout exactly as before;
    - multiple sinks each receive every line in declared order;
    - debug lines are gated by `debug_enabled` and tagged `[debug]`;
    - the file sink appends one line per call without explicit flush;
    - the queue sink drops on full and never raises (TUI never stalls
      a worker thread).
"""
import io
import queue
import sys
from datetime import date, timedelta

from log import (
    Logger,
    _stdout_sink,
    file_sink,
    queue_sink,
    render_log_path,
    rotating_file_sink,
)


def test_default_logger_writes_stdout_only(capsys):
    log = Logger()
    log("hello world")
    captured = capsys.readouterr()
    assert "hello world" in captured.out
    assert captured.err == ""


def test_multi_sink_dispatches_in_order():
    seen_a: list[str] = []
    seen_b: list[str] = []
    sink_a = lambda s: seen_a.append(s)
    sink_b = lambda s: seen_b.append(s)
    log = Logger(sinks=[sink_a, sink_b])

    log("x")

    assert len(seen_a) == 1 and len(seen_b) == 1
    assert seen_a[0] == seen_b[0]                  # identical line
    assert seen_a[0].endswith(" x")                # timestamp prefix + msg


def test_debug_skipped_when_disabled():
    seen: list[str] = []
    log = Logger(debug_enabled=False, sinks=[seen.append])
    log.debug("noisy detail")
    assert seen == []


def test_debug_calls_sinks_when_enabled():
    seen_a: list[str] = []
    seen_b: list[str] = []
    log = Logger(debug_enabled=True, sinks=[seen_a.append, seen_b.append])
    log.debug("noisy detail")
    assert len(seen_a) == 1 and len(seen_b) == 1
    assert "[debug]" in seen_a[0]
    assert "noisy detail" in seen_a[0]


def test_file_sink_appends_line_per_call(tmp_path):
    p = tmp_path / "log.txt"
    sink = file_sink(str(p))
    log = Logger(sinks=[sink])

    log("first")
    log("second")

    text = p.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0].endswith(" first")
    assert lines[1].endswith(" second")


def test_queue_sink_drops_when_queue_full():
    q: queue.Queue = queue.Queue(maxsize=2)
    sink = queue_sink(q)
    log = Logger(sinks=[sink])

    log("a")
    log("b")
    log("c")            # would block on full queue — must drop

    # Drain: only the first two lines should be present.
    drained = []
    while True:
        try:
            drained.append(q.get_nowait())
        except queue.Empty:
            break

    assert len(drained) == 2
    assert any(line.endswith(" a") for line in drained)
    assert any(line.endswith(" b") for line in drained)


def test_queue_sink_does_not_raise_on_closed_queue():
    """If the queue is shut down (3.13+) or otherwise unusable the sink
    must swallow the exception so the producing worker isn't stalled or
    crashed by a broken UI."""
    class _BrokenQueue:
        def put_nowait(self, _item) -> None:
            raise RuntimeError("queue is closed")

    sink = queue_sink(_BrokenQueue())
    log = Logger(sinks=[sink])
    log("safe")  # must not raise


def test_stdout_sink_uses_print(capsys):
    _stdout_sink("direct")
    captured = capsys.readouterr()
    assert "direct" in captured.out


def test_render_log_path_substitutes_date():
    d = date(2026, 5, 4)
    assert render_log_path("log.{date}.txt", d) == "log.2026-05-04.txt"
    assert render_log_path("logs/{date}/run.log", d) == "logs/2026-05-04/run.log"


def test_render_log_path_passes_through_when_no_placeholder():
    assert render_log_path("log.txt") == "log.txt"


def test_rotating_sink_falls_back_when_no_placeholder(tmp_path):
    p = tmp_path / "static.log"
    sink = rotating_file_sink(str(p), retention_days=5)
    log = Logger(sinks=[sink])
    log("alpha")
    log("beta")
    text = p.read_text(encoding="utf-8").splitlines()
    assert len(text) == 2 and text[0].endswith(" alpha") and text[1].endswith(" beta")


def test_rotating_sink_writes_to_today_file(tmp_path):
    today = date(2026, 5, 4)
    template = str(tmp_path / "log.{date}.txt")
    sink = rotating_file_sink(template, retention_days=5, _today=lambda: today)
    log = Logger(sinks=[sink])

    log("hello")

    expected = tmp_path / "log.2026-05-04.txt"
    assert expected.exists()
    assert expected.read_text(encoding="utf-8").strip().endswith(" hello")


def test_rotating_sink_rolls_over_on_date_change(tmp_path):
    template = str(tmp_path / "log.{date}.txt")
    now = {"d": date(2026, 5, 4)}
    sink = rotating_file_sink(template, retention_days=5, _today=lambda: now["d"])
    log = Logger(sinks=[sink])

    log("day1")
    now["d"] = date(2026, 5, 5)
    log("day2")

    f1 = tmp_path / "log.2026-05-04.txt"
    f2 = tmp_path / "log.2026-05-05.txt"
    assert f1.read_text(encoding="utf-8").strip().endswith(" day1")
    assert f2.read_text(encoding="utf-8").strip().endswith(" day2")


def test_rotating_sink_prunes_files_older_than_retention(tmp_path):
    template = str(tmp_path / "log.{date}.txt")
    today = date(2026, 5, 4)

    # Pre-populate dated files: today, -1d, -4d (kept), -5d, -10d (pruned).
    keep_dates = [today, today - timedelta(days=1), today - timedelta(days=4)]
    drop_dates = [today - timedelta(days=5), today - timedelta(days=10)]
    for d in keep_dates + drop_dates:
        (tmp_path / f"log.{d.isoformat()}.txt").write_text("seed\n", encoding="utf-8")

    # An unrelated file with the same prefix but a non-date middle must survive.
    unrelated = tmp_path / "log.notes.txt"
    unrelated.write_text("keep me\n", encoding="utf-8")

    sink = rotating_file_sink(template, retention_days=5, _today=lambda: today)
    log = Logger(sinks=[sink])
    log("trigger prune")

    for d in keep_dates:
        assert (tmp_path / f"log.{d.isoformat()}.txt").exists(), d
    for d in drop_dates:
        assert not (tmp_path / f"log.{d.isoformat()}.txt").exists(), d
    assert unrelated.exists()

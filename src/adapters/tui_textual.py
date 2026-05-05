"""Textual TUI for `monitor.py`.

Single-screen dashboard with six summary widgets + a live log tail. Each
widget consumes a frozen dataclass produced by `TuiMetrics`; widgets do
not run SQL and do not import workers / filters / core. Dashboard widget
focus + number keys drill into per-widget detail screens; Esc returns
to the dashboard.

Imports allowed: `textual`, stdlib, `adapters.tui_metrics`.
"""
from __future__ import annotations

import os
import queue
import threading
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
)

from adapters.tui_metrics import (
    AdapterHealth,
    CascadeCounts,
    CascadeRow,
    DayCounts,
    MatchRow,
    PipelineCounts,
    SignalRow,
    TodayCounts,
    TuiMetrics,
)


# Map dashboard widget id → drill target screen name. Keys must match
# the `id=` used in `DashboardScreen.compose`.
_DRILL_FOR_ID: dict[str, str] = {
    "pipeline": "queue",
    "cascade":  "cascade",
    "signals":  "signals",
    "today":    "today",
    "adapters": "adapters",
    "matches":  "matches",
}


# ── Dashboard widgets ─────────────────────────────────────────────────────
class _Card(Static):
    """Static + can_focus so Tab cycles between dashboard widgets."""
    can_focus = True


class PipelineWidget(_Card):
    """Pacer → Harvester → Sifter → Notifier with queue depths."""

    def update(self, c: PipelineCounts) -> None:  # type: ignore[override]
        next_min = c.next_scan_seconds // 60
        next_sec = c.next_scan_seconds % 60
        body = (
            f"Pacer  >  Harvester  >  Sifter   >  Notifier\n"
            f"{c.pacer_state:<8} q={c.harv_pending:<6} q={c.sift_pending:<5} q={c.noti_pending}\n"
            f"5m: harvest={c.last_5m_harvest}  sift={c.last_5m_sift}  notified={c.last_5m_notified}\n"
            f"next scan in {next_min}m{next_sec:02d}s"
        )
        super().update(body)


class CascadeWidget(_Card):
    def update(self, c: CascadeCounts) -> None:  # type: ignore[override]
        body = (
            f"prefilter  {c.prefilter_in:>6} -> {c.prefilter_kept:<6} "
            f"({c.prefilter_in - c.prefilter_kept} dropped)\n"
            f"bayes      {c.bayes_in:>6} -> {c.bayes_kept:<6} "
            f"({c.bayes_in - c.bayes_kept} confident-no)\n"
            f"llm        {c.llm_in:>6} -> {c.llm_kept:<6} "
            f"({c.llm_in - c.llm_kept} not-target)"
        )
        super().update(body)


class SignalsWidget(_Card):
    def update(self, rows: list[SignalRow]) -> None:  # type: ignore[override]
        if not rows:
            super().update("(no signals loaded)")
            return
        lines = []
        for r in rows[:8]:
            model = "trained" if r.has_model else "none"
            lines.append(
                f"  {r.name:<22} {r.hits_24h:>4}  "
                f"{r.pos_samples}/{r.neg_samples:<6}  {model}"
            )
        super().update("\n".join(lines))


class TodayWidget(_Card):
    def update(self, t: TodayCounts) -> None:  # type: ignore[override]
        body = (
            f"items     {t.items_seen:>6}\n"
            f"match     {t.matches_notified:>6}   llm  {t.llm_calls}\n"
            f"retrains  {t.bayes_retrains:>6}"
        )
        super().update(body)


class AdaptersWidget(_Card):
    def update(self, rows: list[AdapterHealth]) -> None:  # type: ignore[override]
        if not rows:
            super().update("(no adapters)")
            return
        lines = []
        for r in rows:
            mark = {"up": "*", "down": "x", "degraded": "!", "unknown": "?"}.get(r.state, "?")
            lines.append(f"  {r.name:<12} {mark} {r.detail}")
        super().update("\n".join(lines))


class MatchesWidget(_Card):
    def update(self, rows: list[MatchRow]) -> None:  # type: ignore[override]
        if not rows:
            super().update("(no matches yet)")
            return
        lines = []
        for r in rows[:8]:
            channel = (r.channel[:14] + "..") if len(r.channel) > 14 else r.channel
            sig = (r.signal[:8])
            lines.append(
                f"  {r.when}  {sig:<8} {channel:<16} {int(r.confidence * 100)}%"
            )
        super().update("\n".join(lines))


class LogTailWidget(RichLog):
    """Bounded ring of log lines (last 500). Auto-follows."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(
            *args,
            max_lines=500,
            highlight=False,
            markup=False,
            wrap=False,
            auto_scroll=True,
            **kwargs,
        )

    def append(self, line: str) -> None:
        self.write(line)


# ── Dashboard ─────────────────────────────────────────────────────────────
class DashboardScreen(Screen):
    """The default screen: six summary widgets + log tail at bottom."""

    DEFAULT_CSS = """
    DashboardScreen {
        layout: grid;
        grid-size: 2 4;
        grid-columns: 2fr 1fr;
        grid-rows: 7 9 1fr 8;
        grid-gutter: 0 1;
    }
    DashboardScreen.compact {
        layout: vertical;
    }
    PipelineWidget, CascadeWidget, SignalsWidget,
    TodayWidget, AdaptersWidget, MatchesWidget {
        border: round $primary 50%;
        padding: 0 1;
    }
    PipelineWidget:focus, CascadeWidget:focus, SignalsWidget:focus,
    TodayWidget:focus, AdaptersWidget:focus, MatchesWidget:focus {
        border: round $accent;
    }
    LogTailWidget {
        column-span: 2;
        border: round $accent 50%;
    }
    DashboardScreen.compact PipelineWidget,
    DashboardScreen.compact CascadeWidget,
    DashboardScreen.compact SignalsWidget,
    DashboardScreen.compact TodayWidget,
    DashboardScreen.compact AdaptersWidget,
    DashboardScreen.compact MatchesWidget {
        height: auto;
        width: 100%;
    }
    DashboardScreen.compact LogTailWidget {
        column-span: 1;
        width: 100%;
        height: 10;
        dock: bottom;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PipelineWidget(id="pipeline")
        yield TodayWidget(id="today")
        yield CascadeWidget(id="cascade")
        yield AdaptersWidget(id="adapters")
        yield SignalsWidget(id="signals")
        yield MatchesWidget(id="matches")
        yield LogTailWidget(id="logtail")
        yield Footer()

    def on_mount(self) -> None:
        # Auto-focus the first widget so Tab works without a prior mouse click.
        try:
            self.query_one("#pipeline", PipelineWidget).focus()
        except Exception:
            pass


# ── Detail screens ────────────────────────────────────────────────────────
class _DetailScreen(Screen):
    """Shared shell: header + title strip + body + log dock + footer."""

    DEFAULT_CSS = """
    _DetailScreen { layout: vertical; }
    _DetailScreen #title { dock: top; height: 1; padding: 0 1; }
    _DetailScreen #body  { height: 1fr; padding: 0 1; }
    _DetailScreen LogTailWidget {
        dock: bottom;
        height: 6;
        border: round $accent 50%;
    }
    _DetailScreen DataTable { height: 1fr; }
    """

    title_text = "Detail"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self.title_text, id="title")
        yield Container(id="body")
        yield LogTailWidget(id="logtail")
        yield Footer()

    def refresh_data(self, metrics: TuiMetrics) -> None:
        """Subclasses override to repopulate `#body` from live metrics."""


class _TableDetailScreen(_DetailScreen):
    """Detail screen whose body is a single DataTable."""

    columns: tuple[str, ...] = ()

    def on_mount(self) -> None:
        table = DataTable(zebra_stripes=True)
        table.cursor_type = "row"
        table.add_columns(*self.columns)
        self.query_one("#body", Container).mount(table)
        self._table = table
        try:
            self.refresh_data(self.app.metrics)  # type: ignore[attr-defined]
        except Exception:
            pass


class QueueScreen(_TableDetailScreen):
    title_text = "Queue Inspector  -  press Esc to go back"
    columns = ("stage", "status", "age", "payload")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.tasks_rows(limit=200):
            self._table.add_row(
                r.stage,
                r.status,
                f"{r.age_seconds}s",
                r.payload_preview,
            )


class CascadeScreen(_TableDetailScreen):
    title_text = "Cascade Detail  -  press Esc to go back"
    columns = ("time", "id", "signal", "decided_by", "label", "title")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.cascade_rows(limit=200):
            label_text = "YES" if r.label == 1 else "no"
            title = (r.title[:60] + "...") if len(r.title) > 60 else r.title
            self._table.add_row(
                r.when,
                str(r.content_id),
                r.signal,
                r.decided_by,
                label_text,
                title,
            )


class SignalsScreen(_TableDetailScreen):
    title_text = "Signals  -  press Esc to go back"
    columns = ("signal", "hits/24h", "pos", "neg", "model")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.signals():
            self._table.add_row(
                r.name,
                str(r.hits_24h),
                str(r.pos_samples),
                str(r.neg_samples),
                "trained" if r.has_model else "none",
            )


class TodayScreen(_TableDetailScreen):
    title_text = "Today / 7-day rollup  -  press Esc to go back"
    columns = ("day", "items", "matches", "llm")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.daily_rollup(days=7):
            self._table.add_row(
                r.day,
                str(r.items),
                str(r.matches),
                str(r.llm),
            )


class AdaptersScreen(_TableDetailScreen):
    title_text = "Adapters Detail  -  press Esc to go back"
    columns = ("adapter", "state", "detail")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.adapters():
            self._table.add_row(r.name, r.state, r.detail)


class MatchesScreen(_TableDetailScreen):
    title_text = "Matches  -  press Esc to go back"
    columns = ("time", "signal", "channel", "title", "conf")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.matches(limit=200):
            title = (r.title[:60] + "...") if len(r.title) > 60 else r.title
            self._table.add_row(
                r.when,
                r.signal,
                r.channel,
                title,
                f"{int(r.confidence * 100)}%",
            )


class LogScreen(_DetailScreen):
    """Full-height log view. Tails the file sink at mount when available."""
    title_text = "Log  -  press Esc to go back"

    DEFAULT_CSS = """
    LogScreen #title { dock: top; height: 1; padding: 0 1; }
    LogScreen #logtail-full {
        height: 1fr;
        border: round $accent 50%;
    }
    """

    def compose(self) -> ComposeResult:  # type: ignore[override]
        yield Header(show_clock=True)
        yield Static(self.title_text, id="title")
        yield LogTailWidget(id="logtail-full")
        yield Footer()

    def on_mount(self) -> None:
        path = getattr(self.app, "log_file_path", None)
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        widget = self.query_one("#logtail-full", LogTailWidget)
        for line in lines[-500:]:
            widget.append(line.rstrip("\n"))


class HelpScreen(Screen):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen #help-body {
        width: 60;
        border: round $primary;
        padding: 1 2;
    }
    """

    HELP = (
        "Godwit Vane - Help\n\n"
        "  1   queue inspector       4   today / 7-day stats\n"
        "  2   cascade detail        5   adapters detail\n"
        "  3   signals detail        6   matches detail\n"
        "  l   full log view         ?   this help\n\n"
        "  Tab           cycle widget focus\n"
        "  Enter         drill into focused widget\n"
        "  Esc           back to dashboard\n"
        "  q             quit (stops workers cleanly)\n\n"
        "                press Esc to close"
    )

    def compose(self) -> ComposeResult:
        yield Static(self.HELP, id="help-body")


# ── App ───────────────────────────────────────────────────────────────────
class VaneTui(App):
    """Default TUI surface for `monitor.py`."""

    TITLE       = "Godwit Vane"
    SUB_TITLE   = "Core runtime"

    BINDINGS = [
        Binding("q",         "quit_app",                "quit"),
        Binding("escape",    "back",                    "back"),
        Binding("?",         "push_screen('help')",     "help"),
        Binding("tab",       "focus_next",              "focus", show=False),
        Binding("shift+tab", "focus_previous",          "focus", show=False),
        Binding("enter",     "drill_focused",           "drill", show=False),
        Binding("1",         "drill('queue')",          "queue"),
        Binding("2",         "drill('cascade')",        "cascade"),
        Binding("3",         "drill('signals')",        "signals"),
        Binding("4",         "drill('today')",          "today"),
        Binding("5",         "drill('adapters')",       "adapters"),
        Binding("6",         "drill('matches')",        "matches"),
        Binding("l",         "drill('log')",            "log"),
    ]

    SCREENS = {
        "help":     HelpScreen,
        "queue":    QueueScreen,
        "cascade":  CascadeScreen,
        "signals":  SignalsScreen,
        "today":    TodayScreen,
        "adapters": AdaptersScreen,
        "matches":  MatchesScreen,
        "log":      LogScreen,
    }

    layout_mode: reactive[str] = reactive("wide")

    def __init__(
        self,
        *,
        metrics: TuiMetrics,
        log_queue: queue.Queue | None,
        on_quit: Callable[[], None],
        exit_event: threading.Event | None = None,
        log_file_path: str | None = None,
    ) -> None:
        super().__init__()
        self.metrics       = metrics
        self.log_queue     = log_queue
        self.on_quit       = on_quit
        self.exit_event    = exit_event
        self.log_file_path = log_file_path
        self._dashboard    = DashboardScreen()

    # ── lifecycle ────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self.push_screen(self._dashboard)
        self.set_interval(1.0, self._tick_metrics)
        self.set_interval(0.1, self._drain_log_queue)
        if self.exit_event is not None:
            self.set_interval(0.5, self._check_exit_event)

    def on_resize(self, event) -> None:  # type: ignore[override]
        cols, rows = event.size.width, event.size.height
        if   cols >= 120 and rows >= 30: self.layout_mode = "wide"
        elif cols >= 80  and rows >= 24: self.layout_mode = "mid"
        else:                            self.layout_mode = "narrow"

    def watch_layout_mode(self, value: str) -> None:
        try:
            if self._dashboard.is_mounted:
                self._dashboard.set_class(value != "wide", "compact")
        except Exception:
            pass

    # ── ticks ────────────────────────────────────────────────────────────
    def _tick_metrics(self) -> None:
        try:
            dash = self._dashboard
            if dash.is_mounted:
                dash.query_one("#pipeline", PipelineWidget).update(self.metrics.pipeline())
                dash.query_one("#cascade",  CascadeWidget).update(self.metrics.cascade())
                dash.query_one("#adapters", AdaptersWidget).update(self.metrics.adapters())
                dash.query_one("#today",    TodayWidget).update(self.metrics.today())
                dash.query_one("#signals",  SignalsWidget).update(self.metrics.signals())
                dash.query_one("#matches",  MatchesWidget).update(self.metrics.matches())
        except Exception:
            # The TUI is a courtesy view; never let a render error
            # crash the whole app. Workers keep running regardless.
            pass

        # Refresh the active detail screen if it implements refresh_data.
        try:
            screen = self.screen
            if isinstance(screen, _DetailScreen) and hasattr(screen, "_table"):
                screen.refresh_data(self.metrics)
        except Exception:
            pass

    def _check_exit_event(self) -> None:
        if self.exit_event is not None and self.exit_event.is_set():
            self.exit()

    def _drain_log_queue(self) -> None:
        if self.log_queue is None:
            return
        widgets = list(self.query(LogTailWidget))
        if not widgets:
            return
        # Drain a finite chunk per tick to bound work.
        for _ in range(200):
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                return
            for w in widgets:
                w.append(line)

    # ── actions ──────────────────────────────────────────────────────────
    def action_drill(self, target: str) -> None:
        if isinstance(self.screen, DashboardScreen):
            self.push_screen(target)
        else:
            # Replace, don't deepen — sibling navigation between
            # detail screens never grows the screen stack.
            self.switch_screen(target)

    def action_drill_focused(self) -> None:
        if not isinstance(self.screen, DashboardScreen):
            return
        focused = self.focused
        if focused is None:
            return
        target = _DRILL_FOR_ID.get(focused.id or "")
        if target:
            self.action_drill(target)

    def action_back(self) -> None:
        if not isinstance(self.screen, DashboardScreen):
            self.pop_screen()

    def action_quit_app(self) -> None:
        try:
            self.on_quit()
        finally:
            self.exit()

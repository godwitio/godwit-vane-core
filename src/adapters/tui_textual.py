"""Textual TUI for `monitor.py`.

Adaptive single-screen dashboard: six summary cards in a 2-column grid
on wide terminals, a single-column stack on medium, and one-line
summaries on compact. The active layout is chosen by
`tui_layout.get_layout()` from the current terminal size. Widgets
consume frozen dataclasses produced by `TuiMetrics`; widgets do not run
SQL and do not import workers / filters / core. Dashboard widget focus
+ number keys drill into per-widget detail screens; Esc returns to the
dashboard.

Imports allowed: `textual`, stdlib, `adapters.tui_metrics`,
`adapters.tui_layout`.
"""
from __future__ import annotations

import os
import queue
import threading
from typing import Callable

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
)

from adapters.tui_layout import LayoutMode, get_layout
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


# ── small formatting helpers ──────────────────────────────────────────────
def _trunc(s: str, n: int) -> str:
    """Truncate `s` to at most `n` glyphs, marking truncation with `…`."""
    if n <= 0:
        return ""
    if len(s) <= n:
        return s
    if n == 1:
        return "…"
    return s[: n - 1] + "…"


def _fmt_short_duration(seconds: int) -> str:
    """Render a seconds count as `12s` / `7m` / `2h`. Negative -> `—`."""
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


def _pacer_label(state: str) -> str:
    return {"running": "scanning", "scheduled": "waiting"}.get(state, "idle")


# ── Dashboard widgets ─────────────────────────────────────────────────────
class _Card(Static):
    """Static + can_focus so Tab cycles between dashboard widgets.

    Each card carries a `compact` flag set by the app whenever the
    layout mode changes; `update()` reads the flag and renders either a
    full body or a one-line summary.
    """
    can_focus = True
    compact: bool = False


class PipelineWidget(_Card):
    """Pacer → Harvester → Sifter → Notifier with queue depths."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Pipeline"

    def update(self, c: PipelineCounts) -> None:  # type: ignore[override]
        state    = _pacer_label(c.pacer_state)
        total_q  = c.harv_pending + c.sift_pending + c.noti_pending
        last_str = _fmt_short_duration(c.last_scan_seconds_ago)
        next_str = (
            _fmt_short_duration(c.next_scan_seconds)
            if c.pacer_state == "scheduled" else "—"
        )

        if self.compact:
            super().update(
                f"Pipeline  {state}  |  q={total_q}  |  "
                f"last {last_str}  |  next {next_str}"
            )
            return

        body = (
            f"Pacer  >  Harvester  >  Sifter  >  Notifier\n"
            f"{state}   q={total_q}   |   last {last_str}   |   next {next_str}\n"
            f"5m   harv={c.last_5m_harvest}   sift={c.last_5m_sift}   "
            f"noti={c.last_5m_notified}"
        )
        super().update(body)


class CascadeWidget(_Card):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Funnel"

    def update(self, c: CascadeCounts) -> None:  # type: ignore[override]
        if self.compact:
            super().update(
                f"Funnel  prefilter {c.prefilter_in}→{c.prefilter_kept}  |  "
                f"bayes {c.bayes_in}→{c.bayes_kept}  |  "
                f"llm {c.llm_in}→{c.llm_kept}"
            )
            return
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
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Signals"

    def update(self, rows: list[SignalRow]) -> None:  # type: ignore[override]
        if self.compact:
            trained = sum(1 for r in rows if r.has_model)
            super().update(f"Signals  {len(rows)} loaded  |  {trained} trained")
            return

        if not rows:
            super().update("(no signals loaded)")
            return

        # The right column on wide layouts is ~30-35% wide, so signal
        # names compete for space with the counts. Truncate aggressively
        # rather than letting names overflow into the next column.
        # Header widths must match the row format below exactly so the
        # column labels line up over their values.
        lines = [
            f"  [dim]{'project':<10} {'signal':<22} {'24h':>4}  "
            f"{'pos/neg':<8}  model[/dim]"
        ]
        for r in rows:
            model    = "trained" if r.has_model else "none"
            project  = _trunc(r.project or "-", 10)
            name     = _trunc(r.name, 22)
            lines.append(
                f"  {project:<10} {name:<22} {r.hits_24h:>4}  "
                f"{r.pos_samples}/{r.neg_samples:<6}  {model}"
            )
        super().update("\n".join(lines))


class TodayWidget(_Card):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Summary"

    def update(self, t: TodayCounts) -> None:  # type: ignore[override]
        if self.compact:
            super().update(
                f"Stats  items {t.items_seen}  |  matches {t.matches_notified}  |  "
                f"llm {t.llm_calls}  |  retrains {t.bayes_retrains}"
            )
            return
        body = (
            f"items     {t.items_seen:>6}\n"
            f"matches   {t.matches_notified:>6}   llm  {t.llm_calls}\n"
            f"retrains  {t.bayes_retrains:>6}"
        )
        super().update(body)


class AdaptersWidget(_Card):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Runtime health"

    def update(self, rows: list[AdapterHealth]) -> None:  # type: ignore[override]
        if self.compact:
            if not rows:
                super().update("Health  (no adapters)")
                return
            short = {"up": "ok", "down": "down", "degraded": "deg", "unknown": "?"}
            parts = [f"{r.name} {short.get(r.state, '?')}" for r in rows]
            super().update("Health  " + "  |  ".join(parts))
            return

        if not rows:
            super().update("(no adapters)")
            return
        # Header aligned with the row format: name<10, 1-char status mark,
        # then free-text detail. The mark glyph column is self-evident.
        lines = [f"  [dim]{'adapter':<10}   detail[/dim]"]
        for r in rows:
            mark   = {"up": "*", "down": "x", "degraded": "!", "unknown": "?"}.get(r.state, "?")
            detail = _trunc(r.detail, 40)
            lines.append(f"  {r.name:<10} {mark} {detail}")
        super().update("\n".join(lines))


class MatchesWidget(_Card):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.border_title = "Recent"

    def update(self, rows: list[MatchRow]) -> None:  # type: ignore[override]
        if self.compact:
            if not rows:
                super().update("Recent  (no matches yet)")
                return
            head = rows[0]
            super().update(
                f"Recent  {len(rows)} shown  |  last {head.when} {head.signal}"
            )
            return

        if not rows:
            super().update("(no matches yet)")
            return
        # Header widths match the row format below.
        lines = [
            f"  [dim]{'time':<5}  {'signal':<8} {'channel':<16} conf[/dim]"
        ]
        for r in rows:
            channel = _trunc(r.channel, 16)
            sig     = _trunc(r.signal, 8)
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
    """The default screen.

    Layout is driven by CSS classes on the screen — `wide`, `medium`,
    `compact` — toggled by `VaneTui.watch_layout_mode`. The CSS keeps
    all layout math in one place; widgets don't know their own size.

    Wide layout (default):
        Row 1: Pipeline | Runtime health (Adapters)
        Row 2: Funnel (Cascade) | Summary (Today)
        Row 3: Signals | Recent (Matches)     ← grows vertically
        Row 4: Logs spanning full width
    """

    DEFAULT_CSS = """
    /* ── shared card style ── */
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
        border: round $accent 50%;
    }

    /* ── wide = unstyled default. No class required, so the dashboard
           renders correctly on first paint before resize fires. ── */
    DashboardScreen {
        layout: grid;
        grid-size: 2 4;
        grid-columns: 2fr 1fr;
        grid-rows: 7 5 1fr 12;
        grid-gutter: 0 1;
    }
    DashboardScreen PipelineWidget,
    DashboardScreen CascadeWidget,
    DashboardScreen SignalsWidget,
    DashboardScreen TodayWidget,
    DashboardScreen AdaptersWidget,
    DashboardScreen MatchesWidget {
        height: 100%;
        width: 100%;
    }
    DashboardScreen LogTailWidget {
        column-span: 2;
        height: 100%;
        width: 100%;
    }

    /* ── medium: single-column stack, full-fidelity widgets ── */
    DashboardScreen.medium {
        layout: vertical;
    }
    DashboardScreen.medium PipelineWidget,
    DashboardScreen.medium CascadeWidget,
    DashboardScreen.medium TodayWidget,
    DashboardScreen.medium AdaptersWidget {
        width: 100%;
        height: auto;
    }
    DashboardScreen.medium SignalsWidget,
    DashboardScreen.medium MatchesWidget {
        width: 100%;
        height: auto;
        min-height: 6;
    }
    DashboardScreen.medium LogTailWidget {
        width: 100%;
        height: 1fr;
        min-height: 6;
    }

    /* ── compact: one-line summaries, log fills the rest ── */
    DashboardScreen.compact {
        layout: vertical;
    }
    DashboardScreen.compact PipelineWidget,
    DashboardScreen.compact CascadeWidget,
    DashboardScreen.compact SignalsWidget,
    DashboardScreen.compact TodayWidget,
    DashboardScreen.compact AdaptersWidget,
    DashboardScreen.compact MatchesWidget {
        width: 100%;
        height: 3;
    }
    DashboardScreen.compact LogTailWidget {
        width: 100%;
        height: 1fr;
        min-height: 4;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # Compose order matches the wide-grid placement; for medium and
        # compact (which use vertical layout) the same order produces
        # the desired top-to-bottom stack.
        yield PipelineWidget(id="pipeline")      # row 1 col 1
        yield AdaptersWidget(id="adapters")      # row 1 col 2
        yield CascadeWidget(id="cascade")        # row 2 col 1
        yield TodayWidget(id="today")            # row 2 col 2
        yield SignalsWidget(id="signals")        # row 3 col 1 — grows
        yield MatchesWidget(id="matches")        # row 3 col 2 — grows
        yield LogTailWidget(id="logtail")        # row 4 full width
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
            title = _trunc(r.title, 63)
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
    columns = ("project", "signal", "hits/24h", "pos", "neg", "model")

    def refresh_data(self, metrics: TuiMetrics) -> None:
        self._table.clear()
        for r in metrics.signals():
            self._table.add_row(
                r.project or "-",
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
            if r.confidence <= 0.0:
                continue
            title = _trunc(r.title, 63)
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
_CARD_IDS = ("pipeline", "adapters", "cascade", "today", "signals", "matches")


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
        self.layout_mode: LayoutMode = "wide"

    # ── lifecycle ────────────────────────────────────────────────────────
    def on_mount(self) -> None:
        self.push_screen(self._dashboard)
        self.set_interval(1.0, self._tick_metrics)
        self.set_interval(0.1, self._drain_log_queue)
        if self.exit_event is not None:
            self.set_interval(0.5, self._check_exit_event)
        # Apply the initial layout once the dashboard is mounted, so the
        # first paint already has the correct mode — don't wait for the
        # first on_resize to fire.
        self.call_after_refresh(self._init_layout)

    def _init_layout(self) -> None:
        size = self.size
        self._apply_layout(get_layout(size.width, size.height))

    def on_resize(self, event) -> None:  # type: ignore[override]
        self._apply_layout(get_layout(event.size.width, event.size.height))

    def _apply_layout(self, mode: LayoutMode) -> None:
        """Set the dashboard's layout class and per-widget compact flag.

        Applied directly rather than through a reactive so that setting
        the same value twice (e.g. on a resize that didn't cross a
        breakpoint) still applies the class — important on first paint,
        where the default attribute already says "wide" and a reactive
        watcher wouldn't fire.
        """
        self.layout_mode = mode
        dash = self._dashboard
        try:
            if not dash.is_mounted:
                return
            # wide is the unstyled default; only medium/compact carry classes.
            dash.set_class(mode == "medium",  "medium")
            dash.set_class(mode == "compact", "compact")
            compact = (mode == "compact")
            for card_id in _CARD_IDS:
                try:
                    dash.query_one(f"#{card_id}", _Card).compact = compact
                except Exception:
                    pass
            # Render once immediately so a layout change doesn't have to
            # wait up to a second for the next metrics tick.
            self._tick_metrics()
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

"""Adaptive layout selection for the dashboard.

`get_layout(width, height)` picks a breakpoint name that the dashboard
applies as a CSS class. Thresholds are intentionally simple so they can
be tuned in one place without touching widget code.

Modes:
    "wide"    -> two-column grid above a full-width log panel.
    "medium"  -> single-column stack, full-fidelity widgets.
    "compact" -> single-column stack, each widget renders a one-line
                 summary instead of its full body.
"""
from __future__ import annotations

from typing import Literal

LayoutMode = Literal["compact", "medium", "wide"]

# Tune by feel. The wide minimum has to fit a 2fr/1fr split where the
# right column still has room for "sqlite  * 353.1MB | WAL 6.2MB"-style
# detail lines without wrapping.
COMPACT_MIN_WIDTH  = 100
COMPACT_MIN_HEIGHT = 28
WIDE_MIN_WIDTH     = 150


def get_layout(width: int, height: int) -> LayoutMode:
    """Pick the dashboard layout for the given terminal dimensions."""
    if width < COMPACT_MIN_WIDTH or height < COMPACT_MIN_HEIGHT:
        return "compact"
    if width < WIDE_MIN_WIDTH:
        return "medium"
    return "wide"

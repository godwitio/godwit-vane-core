"""In-memory liveness state for adapters that talk to external services.

Each adapter calls `note_ok(name)` after a successful round-trip and
`note_err(name, detail)` when a call fails. The TUI reads back via `get(name)`
to report adapter health on its 1 Hz tick.

State is process-local and intentionally not persisted: a heartbeat is only
meaningful for the running process. If the process restarts, every adapter
goes back to "unknown" until it makes its first call.
"""
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class HeartbeatState:
    ok: bool          # True if the last call succeeded, False if it errored
    at: float         # unix timestamp of the last note
    detail: str       # error message when ok is False; empty when ok


_STATE: dict[str, HeartbeatState] = {}


def note_ok(name: str) -> None:
    _STATE[name] = HeartbeatState(ok=True, at=time.time(), detail="")


def note_err(name: str, detail: str) -> None:
    _STATE[name] = HeartbeatState(ok=False, at=time.time(), detail=str(detail))


def get(name: str) -> HeartbeatState | None:
    return _STATE.get(name)


def reset() -> None:
    """Test helper: clear all state."""
    _STATE.clear()

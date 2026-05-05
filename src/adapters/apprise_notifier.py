from dataclasses import dataclass
from typing import Callable

import apprise

from core.models import RadarHit, SignalHit
from ports.notifier import NotifierPort


@dataclass
class AppriseConfig:
    urls:  list[str]
    title: str = "Godwit Vane"


class AppriseNotifier(NotifierPort):

    def __init__(self, config: AppriseConfig, signals: dict,
                 logger: Callable[[str], None]):
        self._cfg     = config
        self._signals = signals
        self._log     = logger

    def send(self, hits, radar_hits, confidence) -> None:
        body = _compose_digest(hits, radar_hits, confidence, self._signals)
        if not body:
            return
        self._dispatch(body)

    def send_raw(self, message: str) -> None:
        if message.strip():
            self._dispatch(message)

    def _dispatch(self, body: str) -> None:
        if not self._cfg.urls:
            self._log("[notifier] no Apprise URLs configured; skipping")
            return
        ap = apprise.Apprise()
        for url in self._cfg.urls:
            ap.add(url)
        ok = ap.notify(body=body, title=self._cfg.title, body_format=apprise.NotifyFormat.MARKDOWN)
        if not ok:
            self._log("[notifier] apprise.notify returned False for one or more URLs")


def _compose_digest(hits: dict[str, list[SignalHit]],
                    radar_hits: list[RadarHit],
                    confidence: dict[str, float],
                    signals: dict) -> str:
    total = sum(len(v) for v in hits.values()) + len(radar_hits)
    if total == 0:
        return ""

    lines: list[str] = [f"**Godwit Vane** — {total} items"]

    for name, items in hits.items():
        if not items:
            continue
        sig = signals.get(name, {})
        # `name` is the composite ID `<project>__<signal>`. Prefer the
        # injected `_project` / `_name` for display so headers read as
        # "godwit / PAIN" rather than "GODWIT__PAIN".
        project    = sig.get("_project", "")
        human_name = sig.get("_name") or name
        label      = sig.get("label", human_name.upper())
        prefix     = f"{project} / " if project else ""
        header = (
            f"{sig.get('emoji', '•')} **{prefix}{label}** ({len(items)})"
        )
        lines.append("")
        lines.append(header)
        for h in items:
            mark = "🧠" if h.decided_by == "bayes" else "🤖"
            score = "" if h.post.score is None else f" · score {h.post.score}"
            title = h.post.title or h.post.parent_title or "(comment)"
            lines.append(f"- `{h.post.channel}` {mark} {title}{score}\n  <{h.post.url}>")

    if radar_hits:
        lines.append("")
        lines.append(f"📡 **RADAR** ({len(radar_hits)})")
        for r in radar_hits:
            score = "" if r.score is None else f" · score {r.score}"
            lines.append(f"- `{r.channel}` **{r.keyword}** — {r.title}{score}\n  <{r.url}>")

    if confidence:
        lines.append("")
        conf = " · ".join(f"{k}={int(v*100)}%" for k, v in confidence.items())
        lines.append(f"_confidence: {conf}_")

    return "\n".join(lines)

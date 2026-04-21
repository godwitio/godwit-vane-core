# Feature: Apprise Notifications
**Status:** Foundation

---

## What & Why

Writing a custom integration per notification channel is maintenance debt.
Apprise (Python library) supports 80+ services in one dependency: Discord,
Slack, Telegram, Pushover, ntfy, email, Matrix, Gotify, and more.

Users configure Apprise URLs in Settings; the Notifier worker composes digests
and calls `apprise.notify()`. No per-service adapter code.

Rationale: [adr/core-007-apprise-notifications.md](../adr/core-007-apprise-notifications.md).

---

## Files

| File | Role |
|------|------|
| `src/adapters/apprise_notifier.py` | `AppriseNotifier(AppriseConfig)` implementing `NotifierPort` |
| `src/ports/notifier.py` | `NotifierPort` — `send(hits, radar_hits, confidence)`, `send_raw(text)` |

---

## Interface

```python
class NotifierPort(ABC):
    @abstractmethod
    def send(self, hits: dict[str, list[SignalHit]],
                   radar_hits: list[RadarHit],
                   confidence: dict[str, float]) -> None: ...

    @abstractmethod
    def send_raw(self, message: str) -> None: ...
```

`send` is for structured signal digests. `send_raw` is for trend reports,
health alerts, and anything that isn't a classification result.

---

## Apprise URL Examples

```
discord://webhook_id/webhook_token
tgram://bot_token/chat_id
slack://token_a/token_b/token_c
pover://user@token
ntfys://ntfy.example.com/my-topic
mailto://user:pass@smtp.example.com?to=me@example.com
gotify://host/token
```

Configured as `APPRISE_URLS` env (comma-separated) or via Settings UI (future).
Multiple URLs fan out — the same digest goes to all configured channels.

---

## Digest Composition

The Notifier batches `SignalHit` and `RadarHit` objects from the notifications
queue into a single digest per batch, grouped by signal:

```
🎯 GODWIT VANE — 3 signals, 1 radar match

🚨 MIGRATION (2)
  • r/aws — "Moving off S3 to R2, need advice" 🤖 (score 47)
    https://reddit.com/r/aws/comments/xxx
  • r/selfhosted — "Backblaze vs Wasabi for photos" 🧠 (score 12)
    https://reddit.com/r/selfhosted/comments/yyy

💡 COMPARISON (1)
  • r/devops — "S3 compatible object stores in 2026" 🧠

📡 RADAR — brand
  • r/selfhosted — "Anyone used <watched term>?" (score 8)
```

`🧠` = Bayes decided. `🤖` = LLM decided. Emoji from signal JSON.

---

## Key Design Decisions

**Batching, not per-post sends.** The Notifier holds pending items until either
`max_batch` size is reached or `batch_timeout` elapses. Prevents notification
storms during high-activity windows.

**Queue-driven, not direct call.** The Sifter enqueues notifications; the
Notifier claims and sends. Failed sends retry automatically. A crashed Notifier
doesn't lose digests.

**Apprise URLs as opaque config.** The adapter doesn't know what services are
configured — it just hands the list to `apprise.notify()`. Adding a new channel
is a URL change, not a code change.

**Both `send` and `send_raw`.** Structured signals go through `send` which
formats the digest. Trend reports, status alerts, and one-off messages go
through `send_raw` which accepts pre-formatted markdown.

---

## Error Handling

```python
try:
    apprise.notify(body=digest_text, title=digest_title)
except Exception as e:
    self._notifications.fail_batch([n.id for n in batch], str(e))
```

Apprise itself handles per-channel errors; failure of one channel doesn't block
others. If `notify()` raises (network down, misconfigured URL), the batch goes
back to `pending` with backoff and retries up to `MAX_ATTEMPTS`.

---

## What the Notifier Does NOT Do

- ❌ Classify — it receives already-decided hits.
- ❌ Format per-channel — Apprise handles channel-specific rendering.
- ❌ Know about Reddit specifically — it sees `SignalHit` objects only.
- ❌ Auto-discover — admins must configure URLs; empty config means silent.

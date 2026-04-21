from dataclasses import dataclass
import anthropic
from core.models import Post
from ports.labeller import LabellerPort


@dataclass
class AnthropicConfig:
    api_key: str
    model:   str = "claude-haiku-4-5-20251001"
    max_tokens: int = 10


class AnthropicLabeller(LabellerPort):

    def __init__(self, config: AnthropicConfig):
        self._cfg    = config
        self._client = anthropic.Anthropic(api_key=config.api_key)

    def label(self, post: Post, prompt: str) -> bool | None:
        try:
            resp = self._client.messages.create(
                model=self._cfg.model,
                max_tokens=self._cfg.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip().upper()
            if text.startswith("YES"): return True
            if text.startswith("NO"):  return False
            return None
        except Exception:
            return None

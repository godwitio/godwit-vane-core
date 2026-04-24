from dataclasses import dataclass
import anthropic
from core.models import Post
from log import Logger
from ports.labeller import LabellerPort


@dataclass
class AnthropicConfig:
    api_key: str
    model:   str = "claude-haiku-4-5-20251001"
    max_tokens: int = 10


class AnthropicLabeller(LabellerPort):

    def __init__(self, config: AnthropicConfig, logger: Logger):
        self._cfg    = config
        self._log    = logger
        self._client = anthropic.Anthropic(api_key=config.api_key)

    def label(self, post: Post, prompt: str) -> bool | None:
        tag = f"[llm:anthropic] {post.source}:{post.id}"
        self._log.debug(f"{tag} -> prompt:\n{prompt}")
        try:
            resp = self._client.messages.create(
                model=self._cfg.model,
                max_tokens=self._cfg.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            self._log.debug(f"{tag} <- raw={raw!r}")
            text = raw.upper()
            if text.startswith("YES"): return True
            if text.startswith("NO"):  return False
            return None
        except Exception as e:
            self._log(f"{tag} error: {e}")
            return None

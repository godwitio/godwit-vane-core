from dataclasses import dataclass
import requests
from core.models import Post
from log import Logger
from ports.labeller import LabellerPort


@dataclass
class OllamaConfig:
    url:   str = "http://localhost:11434"
    model: str = "qwen2.5:7b"
    timeout: float = 60.0


class OllamaAdapter(LabellerPort):

    def __init__(self, config: OllamaConfig, logger: Logger):
        self._cfg = config
        self._log = logger

    def label(self, post: Post, prompt: str, gate: str = "") -> bool | None:
        gate_suffix = f":{gate}" if gate else ""
        tag = f"[llm:ollama{gate_suffix}] {post.source}:{post.id}"
        self._log.debug(f"{tag} -> prompt:\n{prompt}")
        # In debug mode, let the model generate freely so the raw log captures
        # its full reasoning; in normal runs, 10 tokens is enough for YES/NO.
        num_predict = 200 if self._log.debug_enabled else 10
        try:
            resp = requests.post(
                f"{self._cfg.url}/api/generate",
                json={
                    "model":  self._cfg.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": num_predict},
                },
                timeout=self._cfg.timeout,
            )
            resp.raise_for_status()
            raw = (resp.json().get("response") or "").strip()
            self._log.debug(f"{tag} <- raw={raw!r}")
            text = raw.upper()
            if text.startswith("YES"): return True
            if text.startswith("NO"):  return False
            return None
        except Exception as e:
            self._log(f"{tag} error: {e}")
            return None

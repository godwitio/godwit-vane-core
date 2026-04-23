from dataclasses import dataclass
import requests
from core.models import Post
from ports.labeller import LabellerPort


@dataclass
class OllamaConfig:
    url:   str = "http://localhost:11434"
    model: str = "qwen2.5:7b"
    timeout: float = 60.0


class OllamaAdapter(LabellerPort):

    def __init__(self, config: OllamaConfig):
        self._cfg = config

    def label(self, post: Post, prompt: str) -> bool | None:
        try:
            resp = requests.post(
                f"{self._cfg.url}/api/generate",
                json={
                    "model":  self._cfg.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 10},
                },
                timeout=self._cfg.timeout,
            )
            resp.raise_for_status()
            text = (resp.json().get("response") or "").strip().upper()
            if text.startswith("YES"): return True
            if text.startswith("NO"):  return False
            return None
        except Exception:
            return None

import hashlib
from dataclasses import dataclass, field


def _hash(title: str, body: str) -> str:
    return hashlib.md5((title + body).encode()).hexdigest()[:8]


@dataclass
class Post:
    id:              str
    source:          str                       # "reddit", "hackernews", ...
    channel:         str                       # subreddit / topic / instance
    kind:            str = "post"              # "post" | "comment"
    title:           str = ""
    body:            str = ""
    author:          str = ""
    url:             str = ""
    created_at:      float = 0.0
    score:           int | None = None
    num_comments:    int | None = None
    parent_title:    str = ""
    source_metadata: dict = field(default_factory=dict)
    content_hash:    str = field(init=False)

    def __post_init__(self):
        self.content_hash = _hash(self.title, self.body)


@dataclass
class SignalHit:
    post:        Post
    signal_name: str
    decided_by:  str              # "bayes" | "llm"
    confidence:  float | None = None


@dataclass
class RadarHit:
    source:    str
    source_id: str
    kind:      str
    channel:   str
    title:     str
    url:       str
    score:     int | None
    keyword:   str
    project:   str = ""

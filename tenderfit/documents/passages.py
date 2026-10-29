import hashlib
import re
from dataclasses import dataclass

from tenderfit.documents.parse import Block
from tenderfit.documents.text import words

MIN_WORDS, MAX_WORDS = 150, 400


@dataclass
class Passage:
    ordinal: int
    page: int | None
    section: str | None
    text: str
    kind: str = "text"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(re.sub(r"\s+", " ", self.text).strip().encode()).hexdigest()


def _split_long(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.;:])\s+", text)
    out, cur = [], []
    for s in sentences:
        if cur and words(" ".join(cur + [s])) > MAX_WORDS:
            out.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return out


def section_label(heading: str) -> str:
    h = heading.strip().split("\n")[0]
    return h if len(h) <= 120 else h[:117] + "..."


def build(blocks: list[Block]) -> list[Passage]:
    passages: list[Passage] = []
    section: str | None = None
    buf: list[str] = []
    buf_page: int | None = None

    def flush():
        nonlocal buf, buf_page
        if buf:
            for chunk in _split_long("\n".join(buf)):
                passages.append(Passage(len(passages), buf_page, section, chunk))
        buf, buf_page = [], None

    for b in blocks:
        if b.heading:
            flush()
            section = section_label(b.text)
            if words(b.text) > 14:
                buf, buf_page = [b.text], b.page
            continue
        if b.table:
            flush()
            passages.append(Passage(len(passages), b.page, section, b.text, "table"))
            continue
        if buf and words("\n".join(buf + [b.text])) > MAX_WORDS and words("\n".join(buf)) >= MIN_WORDS:
            flush()
        if not buf:
            buf_page = b.page
        buf.append(b.text)
    flush()
    return passages

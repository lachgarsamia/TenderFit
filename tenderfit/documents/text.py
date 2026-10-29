import re
import unicodedata

from rapidfuzz import fuzz

_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        " ": " ", "•": " ", "": " "})


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_TRANS).lower()
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def words(s: str) -> int:
    return len(s.split())


QUOTE_THRESHOLD = 95


def quote_score(quote: str, source: str) -> float:
    q, s = normalise(quote), normalise(source)
    if not q or len(q) < 12:
        return 0.0
    return fuzz.partial_ratio(q, s)


def quote_ok(quote: str, source: str) -> bool:
    return quote_score(quote, source) >= QUOTE_THRESHOLD

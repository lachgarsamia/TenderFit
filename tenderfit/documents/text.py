import re
import unicodedata

_TRANS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        " ": " ", "•": " ", "": " "})


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_TRANS).lower()
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def words(s: str) -> int:
    return len(s.split())

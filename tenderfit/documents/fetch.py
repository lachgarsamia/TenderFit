import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

MAX_BYTES = 25 * 1024 * 1024
FILE_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
OTHER_FILE_HINTS = ("msword", "excel", "spreadsheet", "zip", "octet-stream", "rtf", "presentation")


@dataclass
class Fetched:
    outcome: str  # file | portal | blocked | unsupported | error
    note: str
    content: bytes = b""
    filename: str | None = None
    content_type: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def _filename(disposition: str, url: str) -> str | None:
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition or "", re.I)
    if m:
        return m.group(1).strip()
    tail = urlparse(url).path.rsplit("/", 1)[-1]
    return tail or None


def sniff_type(content: bytes, content_type: str, filename: str | None) -> str | None:
    if content[:5] == b"%PDF-":
        return ".pdf"
    if content[:2] == b"PK" and (filename or "").lower().endswith(".docx") or "wordprocessingml" in content_type:
        return ".docx"
    return FILE_TYPES.get(content_type.split(";")[0].strip())


def fetch(url: str, http: httpx.Client | None = None) -> Fetched:
    if not url or urlparse(url).scheme not in ("http", "https"):
        return Fetched("error", "no usable link in the notice")
    client = http or httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "TenderFit/0.0.1"})
    try:
        with client.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "").lower()
            disp = r.headers.get("content-disposition", "")
            if r.status_code in (401, 403):
                return Fetched("blocked", f"source refused automated access (HTTP {r.status_code}); upload it instead")
            if r.status_code >= 400:
                return Fetched("error", f"HTTP {r.status_code}")
            is_file = "attachment" in disp.lower() or ctype.split(";")[0] in FILE_TYPES or any(h in ctype for h in OTHER_FILE_HINTS)
            if not is_file:
                return Fetched("portal", "link opens a web page (likely a buyer portal needing registration)")
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    return Fetched("error", "file larger than 25 MB")
    except httpx.HTTPError as e:
        return Fetched("error", f"could not connect ({type(e).__name__})")
    name = _filename(disp, str(url))
    ext = sniff_type(bytes(body), ctype, name)
    if not ext:
        return Fetched("unsupported", f"file type not supported yet ({ctype or 'unknown'})", bytes(body), name, ctype)
    return Fetched("file", "downloaded", bytes(body), name, ctype)

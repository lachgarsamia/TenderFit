import io
import re
from dataclasses import dataclass

import docx
import pymupdf

MIN_TEXT_CHARS = 200
HEADING = re.compile(
    r"^(?:(?:section|part)\s+\d+[a-z]?|(?:appendix|annex|schedule|annexe)\s+[a-z0-9]{1,3}\b|\d{1,2}(?:\.\d{1,2}){0,3}\.?)\s*[-:–.]?\s+\S",
    re.I,
)


@dataclass
class Block:
    text: str
    page: int | None
    heading: bool = False
    table: bool = False


@dataclass
class Parsed:
    blocks: list[Block]
    page_count: int | None
    unreadable: str | None = None


def is_heading(text: str) -> bool:
    t = text.strip()
    return len(t.split()) <= 14 and not t.endswith((",", ";")) and bool(HEADING.match(t))


def _table_text(rows: list[list]) -> str:
    return "\n".join(" | ".join((c or "").replace("\n", " ").strip() for c in row) for row in rows if any(row))


def parse_pdf(content: bytes) -> Parsed:
    doc = pymupdf.open(stream=content, filetype="pdf")
    blocks: list[Block] = []
    total = 0
    for pno, page in enumerate(doc, start=1):
        tables = []
        try:
            tables = list(page.find_tables())
        except Exception:
            tables = []
        boxes = [pymupdf.Rect(t.bbox) for t in tables]
        items = []
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks", sort=True):
            if any(pymupdf.Rect(x0, y0, x1, y1).intersects(b) for b in boxes):
                continue
            text = re.sub(r"[ \t]+", " ", text).strip()
            if text:
                items.append((y0, Block(text, pno, heading=is_heading(text))))
        for t, box in zip(tables, boxes):
            text = _table_text(t.extract())
            if text:
                items.append((box.y0, Block(text, pno, table=True)))
        for _, b in sorted(items, key=lambda i: i[0]):
            total += len(b.text)
            blocks.append(b)
    if total < MIN_TEXT_CHARS:
        return Parsed([], doc.page_count, "no text layer (scanned PDF?); OCR is not supported in version 1")
    return Parsed(blocks, doc.page_count)


def parse_docx(content: bytes) -> Parsed:
    d = docx.Document(io.BytesIO(content))
    blocks: list[Block] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = docx.text.paragraph.Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "").lower()
            blocks.append(Block(text, None, heading=style.startswith(("heading", "title")) or is_heading(text)))
        elif tag == "tbl":
            t = docx.table.Table(child, d)
            text = _table_text([[c.text for c in r.cells] for r in t.rows])
            if text:
                blocks.append(Block(text, None, table=True))
    if sum(len(b.text) for b in blocks) < MIN_TEXT_CHARS:
        return Parsed([], None, "document has almost no text")
    return Parsed(blocks, None)


def parse(content: bytes, ext: str) -> Parsed:
    try:
        if ext == ".pdf":
            return parse_pdf(content)
        if ext == ".docx":
            return parse_docx(content)
    except Exception as e:
        return Parsed([], None, f"could not parse file ({type(e).__name__})")
    return Parsed([], None, f"unsupported file type {ext}")

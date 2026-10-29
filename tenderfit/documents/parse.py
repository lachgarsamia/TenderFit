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

STRONG_HEADING = re.compile(r"^(?:(?:section|part)\s+\d+|(?:appendix|annex|schedule|annexe)\s+[a-z0-9]{1,3}\b|\d{1,2}\.\d{1,2}(?:\.\d{1,2})*)"
                            r"\.?\s*[-:–.]?\s+[A-Z]", re.I)


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


def _styled_heading(text: str, size: float, bold: bool, body_size: float) -> bool:
    t = text.strip()
    if not t or len(t.split()) > 10 or t.endswith((".", ",", ";", ":")) or not t[0].isalnum():
        return False
    return (bold or size >= body_size + 1.5) and any(c.isalpha() for c in t)


def parse_pdf(content: bytes) -> Parsed:
    doc = pymupdf.open(stream=content, filetype="pdf")
    pages = []
    sizes: list[float] = []
    for page in doc:
        try:
            tables = list(page.find_tables())
        except Exception:
            tables = []
        boxes = [pymupdf.Rect(t.bbox) for t in tables]
        items = []
        for b in page.get_text("dict", sort=True)["blocks"]:
            if b.get("type") != 0 or any(pymupdf.Rect(b["bbox"]).intersects(x) for x in boxes):
                continue
            for line in b["lines"]:
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = re.sub(r"[ \t]+", " ", " ".join(s["text"] for s in spans)).strip()
                bold = all(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans)
                sizes += [s["size"] for s in spans for _ in range(max(1, len(s["text"]) // 20))]
                items.append((line["bbox"][1], text, max(s["size"] for s in spans), bold, False))
        for t, box in zip(tables, boxes):
            text = _table_text(t.extract())
            if text:
                items.append((box.y0, text, 0.0, False, True))
        pages.append(sorted(items, key=lambda i: i[0]))
    body = sorted(sizes)[len(sizes) // 2] if sizes else 10.0
    blocks: list[Block] = []
    for pno, items in enumerate(pages, start=1):
        for _, text, size, bold, table in items:
            styled = bold or size >= body + 1.0
            heading = not table and len(text.split()) <= 14 and not text.endswith(".") and (
                bool(STRONG_HEADING.match(text)) or _styled_heading(text, size, bold, body) or (styled and is_heading(text)))
            last = blocks[-1] if blocks else None
            if not heading and not table and last and not last.heading and not last.table and last.page == pno:
                last.text += "\n" + text
            else:
                blocks.append(Block(text, pno, heading=heading, table=table))
    if sum(len(b.text) for b in blocks) < MIN_TEXT_CHARS:
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

import io
import re
from dataclasses import dataclass, field

import docx
import pymupdf
from docx.table import Table
from docx.text.paragraph import Paragraph

MIN_TEXT_CHARS = 200
MIN_PAGE_CHARS = 40          # fewer than this is a page number or a stamp, not a page of text
MIN_VISUAL_COVERAGE = 0.2   # share of a page covered by images or drawings before it counts as a scan
OCR_NOTE = "OCR is not available in this version"
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
    pages: list[dict] = field(default_factory=list)  # per page: {"page", "chars", "status": text|needs_ocr|blank}

    @property
    def ocr_pages(self) -> list[int]:
        return [p["page"] for p in self.pages if p["status"] == "needs_ocr"]


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


def _visual_coverage(page) -> float:
    """Share of the page under images or filled drawings. A scan is one big image; some scanners produce vector fills."""
    area = abs(page.rect) or 1.0
    boxes = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    boxes += [pymupdf.Rect(d["rect"]) for d in page.get_drawings() if d.get("fill") is not None]
    return min(1.0, sum(abs(b & page.rect) for b in boxes) / area)


def _page_status(page) -> dict:
    chars = len("".join(page.get_text().split()))
    if chars >= MIN_PAGE_CHARS:
        status = "text"
    else:
        status = "needs_ocr" if _visual_coverage(page) >= MIN_VISUAL_COVERAGE else "blank"
    return {"page": page.number + 1, "chars": chars, "status": status}


def page_ranges(pages: list[int]) -> str:
    """[2, 3, 4, 9] becomes '2-4, 9'."""
    out: list[str] = []
    for p in sorted(pages):
        if out and out[-1].split("-")[-1] == str(p - 1):
            out[-1] = f"{out[-1].split('-')[0]}-{p}"
        else:
            out.append(str(p))
    return ", ".join(out)


def parse_pdf(content: bytes) -> Parsed:
    doc = pymupdf.open(stream=content, filetype="pdf")
    if doc.needs_pass:
        return Parsed([], doc.page_count, "the PDF is password-protected, so it could not be read; upload an unprotected copy")
    pages = []
    sizes: list[float] = []
    page_info = []
    for page in doc.pages():
        page_info.append(_page_status(page))
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
    needs_ocr = [p["page"] for p in page_info if p["status"] == "needs_ocr"]
    text_pages = [p for p in page_info if p["status"] == "text"]
    if not text_pages and needs_ocr:
        return Parsed([], doc.page_count, f"every page is an image (a scanned PDF), so this needs OCR; {OCR_NOTE}", page_info)
    if sum(len(b.text) for b in blocks) < MIN_TEXT_CHARS:
        if needs_ocr:
            return Parsed([], doc.page_count, f"almost all of this is images, so it needs OCR (pages {page_ranges(needs_ocr)}); {OCR_NOTE}", page_info)
        return Parsed([], doc.page_count, "the document has almost no text", page_info)
    return Parsed(blocks, doc.page_count, None, page_info)


def parse_docx(content: bytes) -> Parsed:
    d = docx.Document(io.BytesIO(content))
    blocks: list[Block] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "").lower()
            blocks.append(Block(text, None, heading=style.startswith(("heading", "title")) or is_heading(text)))
        elif tag == "tbl":
            t = Table(child, d)
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
        return Parsed([], None, f"the file could not be opened; it may be damaged ({type(e).__name__})")
    return Parsed([], None, f"unsupported file type {ext}")

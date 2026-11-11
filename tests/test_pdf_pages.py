"""A PDF is judged page by page: one page of text does not make a scanned document readable."""

import pymupdf
import pytest

from tenderfit.documents import service
from tenderfit.documents.inventory import RefState, coverage
from tenderfit.documents.parse import MIN_PAGE_CHARS, page_ranges, parse
from tests.test_document_service import _portal, _version

TEXT = ("The supplier shall deliver all desks within 30 days of order, and shall provide a full installation service at every site. "
        "This clause forms part of the tender pack and must be read together with the instructions issued by the buyer. ")


def _scan_image() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 560), False)
    pix.clear_with(200)
    return pix.tobytes("png")


def pdf(*kinds: str, password: str | None = None) -> bytes:
    doc = pymupdf.open()
    for kind in kinds:
        page = doc.new_page()
        if kind == "text":
            page.insert_textbox(pymupdf.Rect(72, 72, 520, 700), TEXT * 3, fontsize=10)
        elif kind == "scan":
            page.insert_image(page.rect, stream=_scan_image())
        elif kind == "scan+number":
            page.insert_image(page.rect, stream=_scan_image())
            page.insert_text((280, 820), "12", fontsize=9)
        elif kind == "vector":
            page.draw_rect(page.rect, fill=(0.8, 0.8, 0.8))
    if password:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    return doc.tobytes()


def statuses(parsed):
    return [p["status"] for p in parsed.pages]


def test_a_text_pdf_is_fully_read():
    p = parse(pdf("text", "text", "text"), ".pdf")
    assert p.unreadable is None and statuses(p) == ["text"] * 3 and p.ocr_pages == [] and p.blocks


def test_a_fully_scanned_pdf_reports_that_it_needs_ocr():
    p = parse(pdf("scan", "scan", "scan"), ".pdf")
    assert "needs OCR" in p.unreadable and "not available" in p.unreadable
    assert p.ocr_pages == [1, 2, 3] and not p.blocks


def test_a_text_cover_page_does_not_make_a_scan_readable():
    p = parse(pdf("text", "scan", "scan", "scan"), ".pdf")
    assert p.unreadable is None and p.ocr_pages == [2, 3, 4] and statuses(p)[0] == "text"
    assert {b.page for b in p.blocks} == {1}, "only real text is used, with its own page number"


def test_a_mixed_pdf_keeps_page_references_for_the_text_it_can_read():
    p = parse(pdf("scan", "text", "scan", "text"), ".pdf")
    assert p.ocr_pages == [1, 3] and {b.page for b in p.blocks} == {2, 4}


def test_a_page_number_on_a_scan_is_not_text():
    p = parse(pdf("text", "scan+number"), ".pdf")
    assert p.ocr_pages == [2] and p.pages[1]["chars"] < MIN_PAGE_CHARS


def test_scans_made_of_vector_fills_are_detected():
    assert parse(pdf("text", "vector"), ".pdf").ocr_pages == [2]


def test_a_blank_page_is_not_a_scan():
    p = parse(pdf("text", "blank", "text"), ".pdf")
    assert statuses(p) == ["text", "blank", "text"] and p.ocr_pages == []


def test_an_empty_document_says_so():
    assert "almost no text" in parse(pdf("blank", "blank"), ".pdf").unreadable


@pytest.mark.parametrize("content", [b"%PDF-1.4 this is not really a pdf", b"%PDF-", b"\x00\x01\x02garbage"])
def test_corrupted_files_give_a_clear_error(content):
    p = parse(content, ".pdf")
    assert p.unreadable and "damaged" in p.unreadable and not p.blocks


def test_password_protected_files_say_so():
    assert "password-protected" in parse(pdf("text", password="secret-pw"), ".pdf").unreadable


def test_page_ranges():
    assert page_ranges([2, 3, 4, 9]) == "2-4, 9" and page_ranges([5]) == "5" and page_ranges([]) == ""


def test_partial_never_counts_as_complete_coverage():
    notice = RefState("notice", True, "read")
    assert coverage([notice, RefState("document", True, "partial")]) == "partial"
    assert coverage([notice, RefState("document", True, "read")]) == "complete"
    assert coverage([notice, RefState("document", False, "partial")]) == "complete", "a supporting document does not hold it back"


def test_end_to_end_a_partly_scanned_upload_is_recorded_page_by_page(db, tmp_path, monkeypatch):
    vid = _version(db)
    service.process_version(db, vid, _portal(tmp_path, monkeypatch))
    original = pdf("text", "scan", "scan")
    out = service.attach_upload(db, vid, "pack.pdf", original, "x")
    assert out["status"] == "partial" and "pages 2-3 of 3" in out["note"] and "OCR" in out["note"]
    d = db.execute("SELECT * FROM document WHERE id = %s", (out["document_id"],)).fetchone()
    assert d["access"] == "read" and d["ocr_pages"] == [2, 3] and [p["status"] for p in d["page_status"]] == ["text", "needs_ocr", "needs_ocr"]
    with open(d["storage_path"], "rb") as f:
        assert f.read() == original, "the original file is preserved"
    inv = service.inventory(db, vid)
    assert inv["needs_ocr"] == ["pack.pdf"] and inv["coverage"] != "complete"
    assert db.execute("SELECT count(*) n FROM passage WHERE document_id = %s", (d["id"],)).fetchone()["n"] >= 1


def test_end_to_end_a_fully_scanned_upload_is_not_read(db, tmp_path, monkeypatch):
    vid = _version(db)
    service.process_version(db, vid, _portal(tmp_path, monkeypatch))
    out = service.attach_upload(db, vid, "scan.pdf", pdf("scan", "scan"), "x")
    assert out["status"] == "unreadable" and "OCR" in out["note"]
    assert db.execute("SELECT count(*) n FROM passage WHERE document_id = %s", (out["document_id"],)).fetchone()["n"] == 0


def test_evidence_that_is_only_an_image_is_reported_as_not_inspected(db, tmp_path, monkeypatch):
    from tenderfit.profile import service as ps
    from tenderfit.profile.seed import seed
    monkeypatch.setattr(ps.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    fact = db.execute("SELECT id FROM profile_fact WHERE profile_id = %s AND attribute = 'certificate'", (atlas,)).fetchone()["id"]
    eid = ps.add_evidence(db, atlas, "certificate", "scan.pdf", pdf("scan"))
    assert not ps.link_support(db, fact, eid, 1, "certified to ISO 9001 by an accredited body")
    v = db.execute("SELECT level, checks FROM fact_verification WHERE fact_id = %s ORDER BY id DESC LIMIT 1", (fact,)).fetchone()
    assert v["level"] == "exists" and v["checks"]["contents"]["result"] == "not_inspected"


def test_a_corrupted_evidence_upload_is_refused_clearly(db, tmp_path, monkeypatch):
    from tenderfit.profile import service as ps
    from tenderfit.profile.seed import seed
    monkeypatch.setattr(ps.settings, "document_dir", str(tmp_path))
    atlas = seed(db)["Atlas Furniture"]
    with pytest.raises(ps.ProfileError, match="damaged"):
        ps.add_evidence(db, atlas, "certificate", "bad.pdf", b"%PDF-1.4 broken")



import httpx

from tenderfit.documents import passages, references
from tenderfit.documents.fetch import fetch
from tenderfit.documents.inventory import RefState, coverage, is_essential
from tenderfit.documents.parse import parse
from tests import northfield


def test_pdf_passages_carry_page_and_section():
    parsed = parse(northfield.pdf(northfield.SPEC_V1), ".pdf")
    ps = passages.build(parsed.blocks)
    delivery = next(p for p in ps if "within 30 days" in p.text)
    assert delivery.page == 6 and delivery.section == "3.2 Delivery"
    std = next(p for p in ps if "BS EN 1729-2" in p.text)
    assert std.page == 5 and std.section == "2.3 Product standard"


def test_scanned_pdf_is_unreadable():
    parsed = parse(northfield.scanned_pdf(), ".pdf")
    assert parsed.unreadable and "scanned" in parsed.unreadable


def test_docx_keeps_table_whole_and_uses_heading_styles():
    ps = passages.build(parse(northfield.itt_docx(), ".docx").blocks)
    table = [p for p in ps if p.kind == "table"]
    assert len(table) == 1 and "Quality | 60%" in table[0].text and "Price | 40%" in table[0].text
    assert next(p for p in ps if "comparable contract" in p.text).section == "5.1 Experience"


def test_long_sections_are_split_within_word_bounds():
    from tenderfit.documents.parse import Block
    body = " ".join(["The supplier shall do the thing."] * 300)
    ps = passages.build([Block("4. Long section", 1, heading=True), Block(body, 1)])
    assert len(ps) > 1 and all(len(p.text.split()) <= passages.MAX_WORDS for p in ps)
    assert {p.section for p in ps} == {"4. Long section"}


def test_reference_parser():
    refs = references.find("Provide a reference using the form at Appendix C, as set out in Section 5.1 "
                           "and the Instructions to Tenderers.")
    assert {(r.kind, r.label) for r in refs} == {("part", "Appendix C"), ("section", "Section 5.1"),
                                                  ("named", "Instructions to Tenderers")}
    assert references.has_unresolved_cue("see Appendix B for details")
    assert not references.has_unresolved_cue("Desks shall be delivered within 30 days.")


def test_essential_flag():
    assert is_essential("technicalSpecifications", "Specification")
    assert is_essential(None, None)
    assert not is_essential("biddingDocuments", "Pricing schedule")
    assert not is_essential("conflictOfInterest", None)


def test_coverage_states():
    notice = RefState("notice", True, "read")
    spec = RefState("document", True, "read")
    itt = RefState("document", True, "not_retrieved")
    pricing = RefState("document", False, "not_retrieved")
    assert coverage([notice]) == "notice_only"
    assert coverage([notice, RefState("document", True, "unreadable")]) == "notice_only"
    assert coverage([notice, spec, itt, pricing]) == "partial"
    assert coverage([notice, spec, pricing]) == "complete"
    assert coverage([notice, spec, RefState("document", True, "confirmed_none")]) == "complete"


def _http(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_fetch_outcomes():
    pdf = northfield.pdf(northfield.SPEC_V1)

    def handler(req):
        if req.url.path == "/blocked":
            return httpx.Response(403)
        if req.url.path == "/login":
            return httpx.Response(200, html="<html>Log in</html>")
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf",
                                                          "content-disposition": 'attachment; filename="Spec.pdf"'})

    h = _http(handler)
    assert fetch("https://x.test/blocked", h).outcome == "blocked"
    assert fetch("https://x.test/login", h).outcome == "portal"
    f = fetch("https://x.test/spec", h)
    assert f.outcome == "file" and f.filename == "Spec.pdf" and f.content == pdf
    assert fetch(None, h).outcome == "error"

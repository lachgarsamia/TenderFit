"""A fictional tender and two fictional suppliers, with dates relative to today so the demo works on any day.

Everything here is invented: the buyer, the suppliers, the documents and the certificates. Nothing is real data."""
import io
from datetime import date, datetime, time, timedelta, timezone

import docx
import pymupdf

OCID = "ocds-test-harrowgate"
BUYER = {"id": "GB-TEST-HARROWGATE", "name": "Harrowgate Education Trust (fictional)"}
PAD = "This clause forms part of the tender pack and is to be read with the Instructions to Tenderers."


def spec_pages(delivery_days: int = 30) -> dict[int, list[tuple[str, str]]]:
    return {
        1: [("1. Introduction", "Harrowgate Education Trust invites tenders for teaching-space furniture in two lots. Lot 1 covers "
             "student desks and Lot 2 covers seating. " + PAD)],
        2: [("2.1 Quantities", "The supplier shall supply 200 student desks for Lot 1. The supplier shall supply 400 chairs for Lot 2. " + PAD)],
        3: [("3.2 Delivery", f"The supplier shall deliver all desks within {delivery_days} days of order. " + PAD),
            ("3.3 Spare parts", "Spare parts shall be available for dispatch within 10 days of a request from the Trust. " + PAD)],
        4: [("3.4 Installation", "Installation of all desks shall take place on Working Days as defined in Appendix B. " + PAD),
            ("4.1 Warranty", "The supplier shall provide a warranty of 5 years on all desks. " + PAD)],
        5: [("5.1 Insurance", "Bidders must hold public liability insurance of at least £5,000,000. "
             "Bidders must hold employers liability insurance of at least £5,000,000. " + PAD)],
        6: [("6.1 Environmental management", "Suppliers must hold ISO 14001 certification, valid throughout the contract period, "
             "issued by an accredited certification body. " + PAD),
            ("6.2 Product standard", "Desks shall comply with BS EN 1729-2 or an equivalent standard accepted by the Trust. "
             "Bidders claiming equivalence must provide test evidence from an accredited laboratory. " + PAD)],
        7: [("Appendix B Definitions", "Working Days means Monday to Friday, excluding public holidays and the school closure weeks "
             "published by the Trust each September. Installation outside Working Days needs written agreement from the Trust.")],
    }


def spec_pdf(delivery_days: int = 30, edit=None) -> bytes:
    """edit rewrites the page text, for controlled amendment experiments."""
    pages = spec_pages(delivery_days)
    if edit:
        pages = {n: [(h, edit(b)) for h, b in items] for n, items in pages.items()}
    doc = pymupdf.open()
    for pno in range(1, 8):
        page = doc.new_page()
        y = 72
        for heading, body in pages[pno]:
            page.insert_text((72, y), heading, fontsize=13, fontname="hebo")
            page.insert_textbox(pymupdf.Rect(72, y + 18, 520, y + 150), body, fontsize=10)
            y += 170
    return doc.tobytes()


def itt_docx() -> bytes:
    d = docx.Document()
    d.add_heading("Instructions to Tenderers", 0)
    d.add_heading("5.1 Experience", 1)
    d.add_paragraph("Bidders must provide details of at least one comparable contract for 150 or more items delivered in the last "
                    "three years, with a customer reference. " + PAD)
    d.add_heading("6.2 Sustainability", 1)
    d.add_paragraph("The share of recycled material in the desks will be scored and is worth 10 points of the quality score. " + PAD)
    d.add_heading("7.1 Warranty terms", 1)
    d.add_paragraph("The supplier shall provide a warranty of 3 years on all desks. " + PAD)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def evidence_pdf(title: str, lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), "FICTIONAL TEST DOCUMENT - created for the TenderFit demonstration, not a real record.", fontsize=8)
    page.insert_text((72, 100), title, fontsize=15)
    page.insert_textbox(pymupdf.Rect(72, 130, 520, 760), "\n\n".join(lines), fontsize=10)
    return doc.tobytes()


def stamp(d: date) -> str:
    return datetime.combine(d, time(12, 0), timezone.utc).isoformat().replace("+00:00", "Z")


def release(today: date, rid: str = "090001-2026", tag: str = "tender", published_days_ago: int = 5, deadline_in: int = 21,
            spec: str = "https://portal.test/harrowgate/Specification_v1.pdf") -> dict:
    contract_end = (today + timedelta(days=500)).isoformat()
    return {
        "ocid": OCID, "id": rid, "date": stamp(today - timedelta(days=published_days_ago)), "tag": [tag], "buyer": BUYER,
        "tender": {
            "title": "Teaching-space furniture 2027", "status": "active",
            "description": "Supply, delivery and installation of student desks and seating for two secondary schools (fictional).",
            "legalBasis": {"id": "2023/54", "scheme": "UKPGA"}, "tenderPeriod": {"endDate": stamp(today + timedelta(days=deadline_in))},
            "value": {"amount": 240000, "currency": "GBP"},
            "lots": [{"id": "1", "title": "Student desks", "description": "Supply and installation of 200 student desks.", "status": "active",
                      "contractPeriod": {"endDate": contract_end}},
                     {"id": "2", "title": "Seating", "description": "Supply of 400 chairs.", "status": "active",
                      "contractPeriod": {"endDate": contract_end}}],
            "items": [{"id": "1", "relatedLot": "1", "quantity": 200, "additionalClassifications": [{"scheme": "CPV", "id": "39160000"}],
                       "deliveryAddresses": [{"region": "UKE"}]},
                      {"id": "2", "relatedLot": "2", "quantity": 400, "additionalClassifications": [{"scheme": "CPV", "id": "39110000"}],
                       "deliveryAddresses": [{"region": "UKE"}]}],
            "documents": [
                {"id": "D-1", "documentType": "technicalSpecifications", "title": "Specification", "url": spec},
                {"id": "D-2", "documentType": "biddingDocuments", "title": "Instructions to Tenderers", "url": "https://portal.test/harrowgate/login"},
                {"id": rid, "documentType": "tenderNotice", "noticeType": "UK4", "url": f"https://www.find-tender.service.gov.uk/Notice/{rid}"},
            ],
        },
    }

"""Three fictional test profiles (spec Appendix A). All names, documents and figures are invented."""
from datetime import date

import pymupdf

from tenderfit.db import connect, migrate
from tenderfit.profile import service as ps

FICTION = "FICTIONAL TEST DOCUMENT - created for TenderFit testing, not a real record."


def evidence_pdf(title: str, lines: list[str]) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 60), FICTION, fontsize=8)
    page.insert_text((72, 100), title, fontsize=15)
    page.insert_textbox(pymupdf.Rect(72, 130, 520, 760), "\n\n".join(lines), fontsize=10)
    return doc.tobytes()


def _evidence(conn, pid, kind, filename, title, lines, fact_id, quote, **kw):
    eid = ps.add_evidence(conn, pid, kind, filename, evidence_pdf(title, lines), **kw)
    assert ps.link_support(conn, fact_id, eid, 1, quote), f"seed quote not found in {filename}"
    return eid


def seed_atlas(conn) -> int:
    pid = ps.upsert_profile(conn, "Atlas Furniture", ["UKE", "UKD", "UKF"],
                            "Fictional furniture maker in Yorkshire: student and office desks, chairs, cabinets, installation.")
    nd120 = ps.add_fact(conn, pid, "product", "Student desk ND-120",
                        {"name": "Student desk ND-120", "code": "ND-120", "category": "student desk", "cpv": ["39160000"]},
                        "declared", "declared", confirmed_at=date(2026, 1, 15))
    _evidence(conn, pid, "spec_sheet", "ND-120_spec_sheet.pdf", "Atlas Furniture - ND-120 Student Desk",
              ["Product code: ND-120. Single student desk, 700 x 500 mm, steel frame, laminate top.",
               "Available in six heights for ages 11 to adult.",
               "Test status: no third-party test report has been issued for this product."],
              nd120, "Single student desk, 700 x 500 mm, steel frame, laminate top.")
    ps.add_fact(conn, pid, "product", "Office desk OD-160",
                {"name": "Office desk OD-160", "code": "OD-160", "category": "office desk", "cpv": ["39130000"]},
                "declared", "declared", confirmed_at=date(2026, 1, 15))
    ps.add_fact(conn, pid, "product", "Task chairs, storage cabinets",
                {"name": "Task chairs and storage cabinets", "category": "seating and storage", "cpv": ["39110000", "39120000"]},
                "declared", "declared", confirmed_at=date(2026, 1, 15))
    ps.add_fact(conn, pid, "service", "Installation service", {"service": "installation"}, "declared", "declared",
                confirmed_at=date(2026, 1, 15))
    ps.add_fact(conn, pid, "region", "Yorkshire & Humber, North West, East Midlands",
                {"itl": ["UKE", "UKD", "UKF"], "names": ["Yorkshire and the Humber", "North West", "East Midlands"]},
                "declared", "declared", confirmed_at=date(2026, 1, 15))
    ps.add_fact(conn, pid, "capacity", "Up to 300 desks within 45 days", {"max_units": 300, "lead_days": 45},
                "declared", "declared, confirmed by operations", confirmed_at=date(2026, 10, 2), confirm_by=date(2026, 11, 1))
    ps.add_fact(conn, pid, "experience", "School contract, 2024 - 180 desks supplied and installed",
                {"description": "School desks supplied and installed", "units": 180, "end": "2024-08-30",
                 "reference_letter": False}, "declared", "declared")
    ps.add_fact(conn, pid, "experience", "Academy fit-out, 2025 - 120 desks",
                {"description": "Academy fit-out", "units": 120, "end": "2025-07-25", "reference_letter": False},
                "declared", "declared")
    reg = ps.add_fact(conn, pid, "registration", "Business registration", {"company_number": "TF000001"},
                      "declared", "file", confirmed_at=date(2026, 1, 15))
    _evidence(conn, pid, "registration", "registration_certificate.pdf", "Certificate of Incorporation (fictional)",
              ["Company name: Atlas Furniture Ltd. Company number TF000001.", "Incorporated on 3 March 2011."],
              reg, "Company name: Atlas Furniture Ltd. Company number TF000001.")
    ps.add_fact(conn, pid, "test_report", "BS EN 1729-2 test report", {"standard": "BS EN 1729-2", "product": "ND-120"},
                "missing", "none on file")
    ps.add_fact(conn, pid, "certificate", "ISO 9001 certificate", {"standard": "ISO 9001"}, "unknown", "not stated")
    ins = ps.add_fact(conn, pid, "insurance", "Public liability insurance",
                      {"type": "public_liability", "amount_gbp": 5_000_000}, "declared", "file")
    _evidence(conn, pid, "insurance", "public_liability_schedule.pdf", "Insurance schedule (fictional)",
              ["Insured: Atlas Furniture Ltd.", "Cover: Public liability, limit of indemnity GBP 5,000,000.",
               "Period of insurance: 1 December 2025 to 30 November 2026."],
              ins, "Cover: Public liability, limit of indemnity GBP 5,000,000.", expires_on=date(2026, 11, 30))
    return pid


def seed_pennine(conn) -> int:
    pid = ps.upsert_profile(conn, "Pennine Seating", ["UKE"],
                            "Fictional seating-only manufacturer, fully certified, delivering in Yorkshire only.")
    for name, code in [("Lecture theatre seating LT-40", "LT-40"), ("Task chair TC-9", "TC-9")]:
        f = ps.add_fact(conn, pid, "product", name, {"name": name, "code": code, "category": "seating", "cpv": ["39110000"]},
                        "declared", "declared", confirmed_at=date(2026, 3, 1))
        _evidence(conn, pid, "spec_sheet", f"{code}_spec.pdf", f"Pennine Seating - {name}",
                  [f"Product code {code}. Upholstered seating for education and offices."], f,
                  f"Product code {code}. Upholstered seating for education and offices.")
    ps.add_fact(conn, pid, "region", "Yorkshire & Humber", {"itl": ["UKE"], "names": ["Yorkshire and the Humber"]},
                "declared", "declared", confirmed_at=date(2026, 3, 1))
    ps.add_fact(conn, pid, "capacity", "Up to 500 chairs within 30 days", {"max_units": 500, "lead_days": 30},
                "declared", "declared", confirmed_at=date(2026, 10, 20))
    iso = ps.add_fact(conn, pid, "certificate", "ISO 9001 certificate", {"standard": "ISO 9001"}, "declared", "file")
    _evidence(conn, pid, "certificate", "iso9001.pdf", "ISO 9001:2015 certificate (fictional)",
              ["This certifies that Pennine Seating Ltd operates a quality management system meeting ISO 9001:2015.",
               "Valid until 31 May 2027."], iso,
              "operates a quality management system meeting ISO 9001:2015", expires_on=date(2027, 5, 31))
    for std in ("BS EN 16139", "BS EN 1728"):
        f = ps.add_fact(conn, pid, "test_report", f"{std} test report", {"standard": std, "product": "TC-9"},
                        "declared", "file")
        _evidence(conn, pid, "test_report", f"{std.replace(' ', '_')}.pdf", f"Test report {std} (fictional)",
                  [f"Product TC-9 tested to {std} by an accredited laboratory. Result: pass."], f,
                  f"Product TC-9 tested to {std} by an accredited laboratory. Result: pass.")
    for desc, units, end in [("University lecture seating", 400, "2025-09-12"), ("Council office chairs", 250, "2024-11-29")]:
        f = ps.add_fact(conn, pid, "experience", f"{desc} - {units} seats",
                        {"description": desc, "units": units, "end": end, "reference_letter": True}, "declared", "declared")
        _evidence(conn, pid, "reference", f"reference_{units}.pdf", "Customer reference (fictional)",
                  [f"We confirm Pennine Seating supplied {units} seats ({desc.lower()}), completed {end}."], f,
                  f"We confirm Pennine Seating supplied {units} seats")
    ins = ps.add_fact(conn, pid, "insurance", "Public liability insurance",
                      {"type": "public_liability", "amount_gbp": 10_000_000}, "declared", "file")
    _evidence(conn, pid, "insurance", "pennine_insurance.pdf", "Insurance schedule (fictional)",
              ["Cover: Public liability, limit of indemnity GBP 10,000,000.", "Period: to 31 March 2027."], ins,
              "Cover: Public liability, limit of indemnity GBP 10,000,000.", expires_on=date(2027, 3, 31))
    return pid


def seed_meridian(conn) -> int:
    pid = ps.upsert_profile(conn, "Meridian Interiors", ["UKI", "UKJ"],
                            "Fictional fit-out contractor in London and the South East; resells furniture, no product test reports.")
    ps.add_fact(conn, pid, "service", "Office and education fit-out, installation", {"service": "installation"},
                "declared", "declared", confirmed_at=date(2026, 2, 1))
    ps.add_fact(conn, pid, "product", "Resold desks and chairs (third-party brands)",
                {"name": "Resold desks and chairs", "category": "desk and seating", "cpv": ["39100000"]},
                "declared", "declared", confirmed_at=date(2026, 2, 1))
    ps.add_fact(conn, pid, "region", "London, South East", {"itl": ["UKI", "UKJ"], "names": ["London", "South East"]},
                "declared", "declared", confirmed_at=date(2026, 2, 1))
    ps.add_fact(conn, pid, "capacity", "Four installation teams", {"teams": 4, "lead_days": 60},
                "declared", "declared", confirmed_at=date(2026, 9, 1))
    for desc, units, end in [("School refurbishment", 600, "2025-04-30"), ("Library fit-out", 150, "2023-06-30")]:
        f = ps.add_fact(conn, pid, "experience", f"{desc} - {units} items",
                        {"description": desc, "units": units, "end": end, "reference_letter": True}, "declared", "declared")
        _evidence(conn, pid, "reference", f"meridian_ref_{units}.pdf", "Customer reference (fictional)",
                  [f"Meridian Interiors completed our {desc.lower()} ({units} items) on {end}."], f,
                  f"Meridian Interiors completed our {desc.lower()} ({units} items)")
    ps.add_fact(conn, pid, "test_report", "Product test reports", {"standard": None}, "missing", "none held")
    iso = ps.add_fact(conn, pid, "certificate", "ISO 9001 certificate", {"standard": "ISO 9001"}, "declared", "file")
    _evidence(conn, pid, "certificate", "meridian_iso.pdf", "ISO 9001:2015 certificate (fictional)",
              ["Meridian Interiors Ltd holds ISO 9001:2015 certification.", "Valid until 30 June 2028."], iso,
              "Meridian Interiors Ltd holds ISO 9001:2015 certification.", expires_on=date(2028, 6, 30))
    ps.add_fact(conn, pid, "insurance", "Public liability insurance", {"type": "public_liability", "amount_gbp": 5_000_000},
                "declared", "declared", valid_until=date(2026, 12, 31))
    return pid


def seed(conn) -> dict:
    existing = {r["name"] for r in conn.execute("SELECT name FROM business_profile")}
    out = {}
    for name, fn in [("Atlas Furniture", seed_atlas), ("Pennine Seating", seed_pennine), ("Meridian Interiors", seed_meridian)]:
        if name not in existing:
            out[name] = fn(conn)
    conn.commit()
    return out


if __name__ == "__main__":
    with connect() as c:
        migrate(c)
        print(seed(c))

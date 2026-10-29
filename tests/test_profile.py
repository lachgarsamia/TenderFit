from datetime import date

from tenderfit.profile import service as ps
from tenderfit.profile.seed import evidence_pdf, seed
from tenderfit.profile.validity import FactView, check, default_confirm_by

TODAY = date(2026, 10, 14)
DEADLINE = date(2026, 11, 6)


def fact(attribute="certificate", status="document_on_file", valid_until=None, confirm_by=None, confirmed_at=None):
    return FactView(attribute, status, valid_until, confirm_by, confirmed_at)


def test_certificate_expiring_before_deadline_is_not_usable():
    v = check(fact(valid_until=date(2026, 11, 1)), today=TODAY, deadline=DEADLINE)
    assert not v.usable and v.stale and "renewed certificate" in v.question


def test_insurance_ending_before_contract_start_asks_question():
    v = check(fact("insurance", valid_until=date(2026, 11, 30)), today=TODAY, deadline=DEADLINE,
              contract_start=date(2027, 1, 4), cover_required_for_contract=True)
    assert v.usable and v.stale and "renewed" in v.question


def test_capacity_confirm_by_is_30_days_and_goes_stale_after_it():
    assert default_confirm_by("capacity", date(2026, 10, 2)) == date(2026, 11, 1)
    f = fact("capacity", "declared", confirm_by=date(2026, 11, 1))
    assert check(f, today=TODAY, deadline=DEADLINE).usable
    late = check(f, today=date(2026, 11, 2), deadline=DEADLINE)
    assert not late.usable and late.stale


def test_expired_fact_is_never_usable():
    v = check(fact(valid_until=date(2026, 10, 1)), today=TODAY, deadline=DEADLINE)
    assert not v.usable and "expired" in v.reason


def test_missing_and_unknown_are_not_usable_and_not_stale():
    for s in ("missing", "unknown"):
        v = check(fact(status=s), today=TODAY, deadline=DEADLINE)
        assert not v.usable and not v.stale


def test_declared_region_reconfirms_yearly():
    assert default_confirm_by("region", date(2026, 1, 15)) == date(2027, 1, 15)


def test_seed_and_support_verification(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ps.settings, "document_dir", str(tmp_path))
    ids = seed(db)
    assert set(ids) == {"Atlas Furniture", "Pennine Seating", "Meridian Interiors"}
    atlas = {f["label"]: f for f in ps.facts(db, ids["Atlas Furniture"])}
    assert atlas["Public liability insurance"]["evidence_status"] == "document_on_file"
    assert atlas["Public liability insurance"]["valid_until"] == date(2026, 11, 30)
    assert atlas["BS EN 1729-2 test report"]["evidence_status"] == "missing"
    assert atlas["Up to 300 desks within 45 days"]["evidence_status"] == "declared"
    assert seed(db) == {}

    eid = ps.add_evidence(db, ids["Atlas Furniture"], "other", "x.pdf", evidence_pdf("X", ["Nothing about ISO here."]))
    iso = atlas["ISO 9001 certificate"]["id"]
    assert not ps.link_support(db, iso, eid, 1, "certified to ISO 9001:2015 by an accredited body")
    assert ps.facts(db, ids["Atlas Furniture"])[0] is not None
    assert {f["label"]: f for f in ps.facts(db, ids["Atlas Furniture"])}["ISO 9001 certificate"]["evidence_status"] == "unknown"


def test_daily_check_marks_expired_and_overdue_facts(db, tmp_path, monkeypatch):
    monkeypatch.setattr(ps.settings, "document_dir", str(tmp_path))
    ids = seed(db)
    pid = ids["Atlas Furniture"]
    stale_now = ps.daily_check(db, date(2026, 10, 29))["stale_facts"]
    meridian = {f["label"]: f for f in ps.facts(db, ids["Meridian Interiors"])}
    assert stale_now == [meridian["Four installation teams"]["id"]]
    stale = set(ps.daily_check(db, date(2026, 12, 1))["stale_facts"])
    by_label = {f["label"]: f for f in ps.facts(db, pid)}
    assert by_label["Up to 300 desks within 45 days"]["id"] in stale
    assert by_label["Public liability insurance"]["id"] in stale
    assert by_label["Public liability insurance"]["needs_update_reason"] == "expired on 2026-11-30"

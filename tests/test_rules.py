from datetime import date

from tenderfit.rules.aggregate import Ref, V, dimensions, eligibility, relevance
from tenderfit.rules.engine import MET, NEEDS, NOT_MET, UNKNOWN, Condition, Context, Fact, evaluate

TODAY = date(2026, 10, 14)
ATLAS = [
    Fact(1, "capacity", "Up to 300 desks within 45 days", {"max_units": 300, "lead_days": 45}, "declared",
         confirm_by=date(2026, 11, 1), confirmed_at=date(2026, 10, 2)),
    Fact(2, "experience", "School contract 2024", {"units": 180, "end": "2024-08-30", "reference_letter": False}, "declared"),
    Fact(3, "experience", "Academy fit-out 2025", {"units": 120, "end": "2025-07-25", "reference_letter": False}, "declared"),
    Fact(4, "test_report", "BS EN 1729-2 test report", {"standard": "BS EN 1729-2"}, "missing"),
    Fact(5, "service", "Installation service", {"service": "installation"}, "declared"),
    Fact(6, "insurance", "Public liability", {"amount_gbp": 5_000_000}, "document_on_file", valid_until=date(2026, 11, 30)),
    Fact(7, "region", "Regions", {"itl": ["UKE", "UKD", "UKF"]}, "declared", confirm_by=date(2027, 1, 15)),
]


def ctx(deadline=date(2026, 10, 30), quantity=200, regions=("UKE",)):
    return Context(TODAY, {"notice.deadline": deadline, "lot.quantity": quantity, "lot.regions": list(regions)})


def ev(cond, category="other", c=None, facts=ATLAS):
    return evaluate(Condition(**cond), facts, c or ctx(), category=category)


def test_quantity_met_on_fresh_declared_capacity():
    r = ev({"kind": "at_least", "fact": "capacity.max_units", "bound": "lot.quantity"}, "quantity")
    assert r.status == MET and r.basis == "declared" and r.params == ["lot.quantity"] and r.facts == [1]


def test_quantity_not_met_only_with_explicit_contrary_evidence():
    r = ev({"kind": "at_least", "fact": "capacity.max_units", "bound": "lot.quantity"}, "quantity", ctx(quantity=1200))
    assert r.status == NOT_MET and r.needs_human == "G6"


def test_delivery_v1_blocks_and_v2_needs_workload_confirmation():
    v1 = ev({"kind": "at_most", "fact": "capacity.lead_days", "bound": 30}, "delivery")
    v2 = ev({"kind": "at_most", "fact": "capacity.lead_days", "bound": 45}, "delivery")
    assert v1.status == NOT_MET
    assert v2.status == NEEDS and v2.needs_human == "G3" and "workload" in v2.reason


def test_stale_contrary_fact_is_not_a_blocker():
    late = Context(date(2026, 11, 5), ctx().fields)
    r = ev({"kind": "at_most", "fact": "capacity.lead_days", "bound": 30}, "delivery", late)
    assert r.status == NEEDS and r.needs_human == "G4"


def test_experience_window_uses_deadline_parameter_and_needs_reference():
    cond = {"kind": "within_years", "fact": "experience.end", "years": 3, "anchor": "notice.deadline", "min_units": 150}
    r = ev(cond, "experience")
    assert r.status == NEEDS and r.needs_human == "G2" and r.facts == [2] and "notice.deadline" in r.params
    later = ev(cond, "experience", ctx(deadline=date(2027, 9, 30)))
    assert later.status == NOT_MET


def test_missing_test_report_is_unknown_not_not_met():
    r = ev({"kind": "has_evidence", "fact": "test_report", "match": "BS EN 1729-2"}, "standard")
    assert r.status == UNKNOWN and r.question


def test_no_fact_at_all_is_unknown():
    r = ev({"kind": "has_evidence", "fact": "certificate", "match": "ISO 14001"}, "certificate")
    assert r.status == UNKNOWN


def test_installation_met_on_declared_and_insurance_met_on_documents():
    assert ev({"kind": "has_evidence", "fact": "service", "match": "installation"}).status == MET
    r = ev({"kind": "min_amount", "fact": "insurance.amount_gbp", "bound": 5_000_000}, "insurance")
    assert r.status == MET and r.basis == "documents"


def test_insurance_expiring_before_deadline_needs_confirmation():
    r = ev({"kind": "min_amount", "fact": "insurance.amount_gbp", "bound": 5_000_000}, "insurance", ctx(deadline=date(2026, 12, 5)))
    assert r.status == NEEDS and r.question


def test_region_coverage():
    assert ev({"kind": "region_covered", "fact": "region"}).status == MET
    assert ev({"kind": "region_covered", "fact": "region"}, c=ctx(regions=("UKI",))).status == NOT_MET


NOTICE = Ref("Tender notice", "notice", True, "read")
SPEC = Ref("Specification", "document", True, "read")
ITT = Ref("Instructions to Tenderers", "document", True, "not_retrieved")


def v(status, obligation="mandatory", category="other"):
    return V(1, "req", obligation, category, status, "r", "Spec §3.2 · p.6")


def test_eligibility_order():
    assert eligibility([v(NOT_MET)], [NOTICE, ITT])[0] == "blocker_found"
    assert eligibility([v(MET)], [NOTICE, ITT])[0] == "not_assessed"
    assert eligibility([v(MET)], [NOTICE, SPEC, ITT])[0] == "provisional"
    assert eligibility([v(MET), v(NEEDS)], [NOTICE, SPEC])[0] == "needs_confirmation"
    assert eligibility([v(MET), v(UNKNOWN, "scored")], [NOTICE, SPEC])[0] == "no_blocker"


def test_provisional_never_says_no_blocker_found():
    d = dimensions([v(MET)], [NOTICE, SPEC, ITT], {"level": "strong", "detail": ""}, "partial")
    assert d.provisional and d.eligibility["label"] == "Provisional"
    assert "no blocker found in documents read (1 of 2)" in d.recommendation


def test_blocker_shown_whatever_the_coverage():
    d = dimensions([v(NOT_MET, category="delivery")], [NOTICE, ITT], {"level": "strong", "detail": ""}, "notice_only")
    assert d.recommendation.startswith("Blocker found") and d.feasibility["level"] == "blocker"


def test_relevance_levels():
    assert relevance(["39160000"], ["39160000"], ["UKE"], ["UKE"])["level"] == "strong"
    assert relevance(["39160000"], ["39110000"], ["UKE"], ["UKE"])["level"] == "partial"
    assert relevance(["33192000"], ["39160000"], ["UKE"], ["UKE"])["level"] == "low"

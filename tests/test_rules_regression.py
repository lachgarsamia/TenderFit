"""Regression table for every false verdict found in the audit, plus properties that must hold for all inputs."""
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tenderfit.rules.engine import MET, NEEDS, NOT_MET, UNKNOWN, Condition, Context, Fact, Outcome, evaluate

TODAY = date(2026, 10, 14)
DEADLINE = date(2026, 11, 20)
VALID = date(2027, 6, 1)


def ctx(**f):
    return Context(TODAY, {"notice.deadline": DEADLINE, "lot.quantity": 200.0, "lot.regions": ["UKE"], "lot.contract_end": date(2028, 3, 31), **f})


PL = Fact(1, "insurance", "Public liability", {"type": "public_liability", "amount_gbp": 10_000_000}, "document_on_file", VALID)
EL = Fact(2, "insurance", "Employers liability", {"type": "employers_liability", "amount_gbp": 1_000_000}, "document_on_file", VALID)
CHAIRS = Fact(3, "capacity", "Up to 500 chairs within 30 days", {"max_units": 500, "lead_days": 30, "scope": "chairs"}, "declared",
              None, date(2026, 11, 30), TODAY)
DESKS = Fact(4, "capacity", "Up to 300 desks", {"max_units": 300, "lead_days": 20, "scope": "desks"}, "document_on_file", None, date(2026, 11, 30), TODAY)
ISO14001 = Fact(5, "certificate", "ISO 14001 certificate", {"standard": "ISO 14001"}, "document_on_file", VALID)
ISO9001 = Fact(6, "certificate", "ISO 9001 certificate", {"standard": "ISO 9001:2015"}, "document_on_file", VALID)
EXP_NO_END = Fact(7, "experience", "School contract", {"description": "School desks"}, "declared")
EXP_NO_UNITS = Fact(8, "experience", "School contract", {"end": "2025-01-01"}, "declared")
EXP_GOOD = Fact(9, "experience", "Academy desks", {"end": "2025-01-01", "units": 400, "reference_letter": True}, "declared")

INS = {"kind": "min_amount", "fact": "insurance.amount_gbp", "bound": 5_000_000}
QTY = {"kind": "at_least", "fact": "capacity.max_units", "bound": "lot.quantity"}
EXP = {"kind": "within_years", "fact": "experience.end", "years": 3, "anchor": "notice.deadline", "min_units": 150}

KNOWN_FALSE_VERDICTS = [
    ("insurance named in the requirement text, wrong policy listed first", INS, [PL, EL], "Employers' liability insurance of £5m", NOT_MET),
    ("the same requirement, policies listed the other way round", INS, [EL, PL], "Employers' liability insurance of £5m", NOT_MET),
    ("insurance qualifier set, only another policy type held", {**INS, "qualifier": "professional indemnity"}, [PL, EL], None, UNKNOWN),
    ("policy type unstated and several held: not settled by rule", INS, [PL, EL], None, UNKNOWN),
    ("capacity for another product must not satisfy", QTY, [CHAIRS], "Supply 200 student desks", UNKNOWN),
    ("capacity qualifier set, other product held", {**QTY, "qualifier": "desks"}, [CHAIRS], None, UNKNOWN),
    ("the matching product among several", QTY, [CHAIRS, DESKS], "Supply 200 student desks", MET),
    ("evidence with no named standard is not proven", {"kind": "has_evidence", "fact": "certificate"}, [ISO14001], None, UNKNOWN),
    ("a different standard does not satisfy", {"kind": "has_evidence", "fact": "certificate", "match": "ISO 9001"}, [ISO14001], None, UNKNOWN),
    ("a standard prefix does not match", {"kind": "has_evidence", "fact": "certificate", "match": "ISO 900"}, [ISO9001], None, UNKNOWN),
    ("a dated edition of the standard does match", {"kind": "has_evidence", "fact": "certificate", "match": "ISO 9001"}, [ISO9001], None, MET),
    ("no end date is not a blocker", EXP, [EXP_NO_END], None, NEEDS),
    ("no unit count is not a blocker", EXP, [EXP_NO_UNITS], None, NEEDS),
    ("an incomplete record does not hide a qualifying one", EXP, [EXP_NO_END, EXP_GOOD], None, MET),
]


@pytest.mark.parametrize("name,cond,facts,text,expected", KNOWN_FALSE_VERDICTS, ids=[c[0] for c in KNOWN_FALSE_VERDICTS])
def test_known_false_verdicts(name, cond, facts, text, expected):
    r = evaluate(Condition(**cond), facts, ctx(), category="other", requirement=text)
    assert r.status == expected, r.reason


@pytest.mark.parametrize("cond", [
    {"kind": "within_years", "fact": "experience.end", "years": 3, "anchor": "2023-01-01x"},
    {"kind": "within_years", "fact": "experience.end", "years": 3, "anchor": "notice.deadline", "min_units": "150 items"},
    {"kind": "within_years", "fact": "experience.end", "years": None, "anchor": "notice.deadline"},
    {"kind": "at_least", "fact": "capacity.max_units", "bound": "two hundred"},
    {"kind": "at_least", "fact": "capacity.max_units", "bound": float("nan")},
    {"kind": "at_least", "fact": "capacity.max_units", "bound": float("inf")},
    {"kind": "at_most", "fact": "capacity.lead_days", "bound": -3},
    {"kind": "min_amount", "fact": "insurance.amount_gbp", "bound": None},
])
def test_invalid_conditions_are_handed_on_not_decided(cond):
    r = evaluate(Condition(**cond), [EXP_GOOD, DESKS, PL], ctx(), requirement="Supply desks with public liability")
    assert r.status == UNKNOWN and r.outcome is Outcome.NEEDS_MODEL


def test_conflicting_facts_need_confirmation_not_a_guess():
    old = Fact(10, "capacity", "Older figure: 100 desks", {"max_units": 100, "scope": "desks"}, "declared", None, date(2026, 11, 30), TODAY)
    r = evaluate(Condition(**QTY), [DESKS, old], ctx(), requirement="Supply 200 desks")
    assert r.status == NEEDS and set(r.facts) == {4, 10} and "conflicting" in r.reason


def test_equivalent_allowed_asks_the_buyer_instead_of_deciding():
    cond = Condition(kind="has_evidence", fact="certificate", match="ISO 9001")
    assert evaluate(cond, [ISO14001], ctx()).status == UNKNOWN
    r = evaluate(cond, [ISO14001], ctx(), equivalent_allowed=True)
    assert r.status == NEEDS and r.needs_human == "G5" and "equivalent" in r.reason


def test_units_are_converted_or_refused():
    fast = Fact(11, "capacity", "Lead time", {"lead_days": 40}, "document_on_file", None, date(2026, 11, 30), TODAY, confirmed_for_lot=True)
    weeks = Condition(kind="at_most", fact="capacity.lead_days", bound=6, unit="weeks")
    assert evaluate(weeks, [fast], ctx()).status == MET
    assert evaluate(Condition(kind="at_most", fact="capacity.lead_days", bound=5, unit="weeks"), [fast], ctx()).status == NOT_MET
    working = evaluate(Condition(kind="at_most", fact="capacity.lead_days", bound=30, unit="working days"), [fast], ctx())
    assert working.status == NEEDS and "working days" in working.reason


def test_validity_through_the_contract_period():
    cond = Condition(kind="valid_through", fact="certificate", match="ISO 9001", anchor="lot.contract_end")
    short = evaluate(cond, [ISO9001], ctx())
    assert short.status == NEEDS and short.params == ["lot.contract_end"] and "before 2028-03-31" in short.reason
    long = Fact(12, "certificate", "ISO 9001 certificate", {"standard": "ISO 9001"}, "document_on_file", date(2029, 1, 1))
    assert evaluate(cond, [long], ctx()).status == MET
    none = Fact(13, "certificate", "ISO 9001 certificate", {"standard": "ISO 9001"}, "document_on_file")
    assert evaluate(cond, [none], ctx()).status == NEEDS


# Properties -----------------------------------------------------------------------------------------------------------

STATUSES = st.sampled_from(["declared", "document_on_file", "missing", "unknown", "needs_update"])
POLICY = st.sampled_from(["public_liability", "employers_liability", "professional_indemnity", "product_liability"])
NUMBER = st.one_of(st.none(), st.integers(0, 10_000_000), st.floats(allow_nan=True, allow_infinity=True), st.text(max_size=6))
DAY = st.dates(date(2020, 1, 1), date(2030, 1, 1))


def facts_strategy():
    insurance = st.builds(lambda i, t, a, s, v: Fact(i, "insurance", t.replace("_", " "), {"type": t, "amount_gbp": a}, s, v),
                          st.integers(1, 10**6), POLICY, NUMBER, STATUSES, st.one_of(st.none(), DAY))
    capacity = st.builds(lambda i, u, l, s, sc: Fact(i, "capacity", "cap", {"max_units": u, "lead_days": l, "scope": sc}, s),
                         st.integers(1, 10**6), NUMBER, NUMBER, STATUSES, st.sampled_from(["desks", "chairs", None]))
    experience = st.builds(lambda i, e, u, s: Fact(i, "experience", "contract", {"end": e, "units": u}, s),
                           st.integers(1, 10**6), st.one_of(st.none(), st.just("2025-01-01"), st.just("not a date")), NUMBER, STATUSES)
    return st.lists(st.one_of(insurance, capacity, experience), max_size=5, unique_by=lambda f: f.id)


CONDITIONS = st.builds(
    Condition, kind=st.sampled_from(["at_least", "at_most", "within_years", "min_amount", "has_evidence", "region_covered", "valid_through"]),
    fact=st.sampled_from(["capacity.max_units", "capacity.lead_days", "experience.end", "insurance.amount_gbp", "certificate", "region"]),
    bound=st.one_of(st.none(), st.floats(allow_nan=True, allow_infinity=True), st.just("lot.quantity"), st.text(max_size=5)),
    years=st.one_of(st.none(), st.integers(-3, 10)), anchor=st.one_of(st.none(), st.just("notice.deadline"), st.text(max_size=5)),
    min_units=st.one_of(st.none(), st.integers(0, 500), st.text(max_size=5)), match=st.one_of(st.none(), st.just("ISO 9001")),
    qualifier=st.one_of(st.none(), st.just("desks"), st.just("public liability")), unit=st.one_of(st.none(), st.just("weeks"), st.just("working days")))


@settings(max_examples=300, deadline=None)
@given(CONDITIONS, facts_strategy())
def test_no_condition_ever_raises_and_reordering_changes_nothing(cond, facts):
    a = evaluate(cond, facts, ctx(), requirement="Supply 200 desks with public liability")
    b = evaluate(cond, list(reversed(facts)), ctx(), requirement="Supply 200 desks with public liability")
    assert (a.status, a.outcome, sorted(a.facts)) == (b.status, b.outcome, sorted(b.facts))


@settings(max_examples=300, deadline=None)
@given(CONDITIONS, st.sampled_from(["missing", "unknown"]))
def test_missing_information_is_never_not_met(cond, status):
    facts = [Fact(1, a, f"{a} fact", {}, status) for a in ("capacity", "experience", "insurance", "certificate", "region")]
    assert evaluate(cond, facts, ctx()).status != NOT_MET
    assert evaluate(cond, [], ctx()).status != NOT_MET
    assert evaluate(cond, [Fact(1, a, "declared, no values", {}, "declared") for a in ("capacity", "experience", "insurance")],
                    ctx()).status != NOT_MET


@settings(max_examples=200, deadline=None)
@given(st.integers(0, 10_000_000), st.integers(1, 10_000_000), POLICY, POLICY)
def test_incompatible_policies_never_satisfy_each_other(amount, bound, have, want):
    from tenderfit.rules.engine import insurance_type_in
    if insurance_type_in(have) == insurance_type_in(want):
        return
    f = Fact(1, "insurance", have.replace("_", " "), {"type": have, "amount_gbp": amount}, "document_on_file", VALID)
    r = evaluate(Condition(kind="min_amount", fact="insurance.amount_gbp", bound=bound, qualifier=want.replace("_", " ")), [f], ctx())
    assert r.status == UNKNOWN and r.outcome is Outcome.NO_EVIDENCE


@settings(max_examples=200, deadline=None)
@given(st.integers(0, 100_000), st.integers(0, 100_000))
def test_unrelated_facts_never_satisfy(units, bound):
    f = Fact(1, "capacity", "cap", {"max_units": units, "scope": "chairs"}, "document_on_file", None, date(2026, 11, 30), TODAY)
    assert evaluate(Condition(kind="at_least", fact="capacity.max_units", bound=bound), [f], ctx(), requirement="Supply student desks").status == UNKNOWN
    assert evaluate(Condition(kind="at_least", fact="capacity.max_units", bound=bound, qualifier="lecterns"), [f], ctx()).status == UNKNOWN

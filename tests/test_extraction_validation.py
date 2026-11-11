"""Adversarial model output: nothing unverified may become a trusted rule condition, and nothing is silently lost."""
import pytest

from tenderfit.documents import service
from tenderfit.extraction.extract import extract_version
from tenderfit.extraction.validate import check_condition, normalise_lot, numbers_in, same_digits
from tenderfit.llm import JsonResult, LLMError, OutputTooLong, Usage
from tests.fakes import ScriptedLLM, passage_id
from tests.test_document_service import _portal, _version
from tests.test_extraction import req


def c(kind, fact, **kw):
    return {"kind": kind, "fact": fact, "bound": None, "years": None, "anchor": None, "min_units": None, "match": None,
            "qualifier": None, "unit": None, **kw}


@pytest.mark.parametrize("text,value", [
    ("within 30 days", 30), ("at least £5m", 5_000_000), ("£5,000,000", 5_000_000), ("5 million pounds", 5_000_000),
    ("thirty days", 30), ("one hundred and fifty items", 150), ("twenty-five", 25), ("two hundred desks", 200), ("three years", 3),
])
def test_numbers_are_read_from_digits_and_words(text, value):
    assert float(value) in numbers_in(text)


def test_digit_sequences_must_match_exactly():
    assert same_digits("within 30 days", "shall deliver within 30 days of order")
    assert not same_digits("within 80 days", "shall deliver within 30 days of order")
    assert same_digits("cover of £5,000,000", "cover of £5000000 is required")


@pytest.mark.parametrize("raw,lot,ok", [("1", "1", True), ("Lot 1", "1", True), ("lot-1", "1", True), (" LOT 2 ", "2", True),
                                        (None, None, True), ("9", "9", False), ("Lot 9", "Lot 9", False)])
def test_lot_identifiers(raw, lot, ok):
    assert normalise_lot(raw, ["1", "2"]) == (lot, ok)


Q_DAYS = "The supplier shall deliver all desks within 30 days of order."
Q_WEEKS = "Delivery within 6 weeks of order."
Q_INS = "Bidders must hold employers liability insurance of at least £5m."
Q_DATE = "Experience gained since 2023-01-01 counts."

ADVERSARIAL = [
    ("wrong numeric threshold", c("at_most", "capacity.lead_days", bound=300), Q_DAYS),
    ("wrong date", c("within_years", "experience.end", years=3, anchor="2019-05-05"), "in the last 3 years since 2023-01-01"),
    ("years not stated", c("within_years", "experience.end", years=5, anchor="notice.deadline"), "in the last three years"),
    ("wrong insurance type", c("min_amount", "insurance.amount_gbp", bound=5_000_000, qualifier="public liability"), Q_INS),
    ("unrecognised insurance type", c("min_amount", "insurance.amount_gbp", bound=5_000_000, qualifier="vehicle cover"), Q_INS),
    ("missing unit", c("at_most", "capacity.lead_days", bound=30), "The lead time must not exceed 30."),
    ("unit changed", c("at_most", "capacity.lead_days", bound=6, unit="days"), Q_WEEKS),
    ("invalid kind", c("somehow", "capacity.max_units", bound=5), Q_DAYS),
    ("kind does not fit the fact", c("at_most", "insurance.amount_gbp", bound=30), Q_DAYS),
    ("text in numeric field", c("at_least", "capacity.max_units", bound="two hundred"), "Supply two hundred desks."),
    ("unknown field", c("at_least", "capacity.max_units", bound="lot.budget"), "Supply two hundred desks."),
    ("evidence with no name", c("has_evidence", "certificate"), "Bidders must hold ISO 9001."),
    ("evidence not in the quote", c("has_evidence", "certificate", match="ISO 14001"), "Bidders must hold ISO 9001."),
    ("foreign currency", c("min_amount", "insurance.amount_gbp", bound=5_000_000), "Public liability cover of €5m."),
    ("similar quote, different number", c("at_least", "capacity.max_units", bound=2000), "Supply 200 desks and 2000 chairs".replace("2000 chairs", "chairs")),
]


@pytest.mark.parametrize("name,raw,quote", ADVERSARIAL, ids=[a[0] for a in ADVERSARIAL])
def test_unverifiable_conditions_are_dropped_with_a_reason(name, raw, quote):
    checked = check_condition(raw, quote)
    assert checked.condition is None and checked.note


def test_valid_conditions_pass_and_are_completed_from_the_quote():
    days = check_condition(c("at_most", "capacity.lead_days", bound=30), Q_DAYS)
    assert days.condition and days.condition["unit"] == "days"
    weeks = check_condition(c("at_most", "capacity.lead_days", bound=6), Q_WEEKS)
    assert weeks.condition["unit"] == "weeks"
    ins = check_condition(c("min_amount", "insurance.amount_gbp", bound=5_000_000), Q_INS)
    assert ins.condition["qualifier"] == "employers liability"
    words = check_condition(c("within_years", "experience.end", years=3, anchor="notice.deadline", min_units=150),
                            "one hundred and fifty items in the last three years")
    assert words.condition
    assert check_condition(c("at_least", "capacity.max_units", bound="lot.quantity"), "Supply the desks.").condition


def setup(db, tmp_path, monkeypatch):
    vid = _version(db)
    service.process_version(db, vid, _portal(tmp_path, monkeypatch))
    return vid


def script(*make):
    def run(role, user, schema):
        return {"requirements": [m(user) for m in make if m(user)]}
    return run


def delivery(statement="Deliver all desks within 30 days of order", quote="The supplier shall deliver all desks within 30 days of order.", **kw):
    def make(user):
        return req(passage_id(user, "within 30 days"), statement, quote, **kw) if "within 30 days" in user else None
    return make


def rejections(db):
    return {r["statement"]: r for r in db.execute("SELECT * FROM requirement_rejection")}


def test_quote_with_a_changed_number_is_rejected_even_though_it_is_similar(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    extract_version(db, vid, ScriptedLLM(script(delivery("Deliver in 80 days", "The supplier shall deliver all desks within 80 days of order."))))
    assert db.execute("SELECT count(*) n FROM requirement").fetchone()["n"] == 0
    assert rejections(db)["Deliver in 80 days"]["kind"] == "quote"


def test_statement_with_an_invented_number_is_rejected(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    extract_version(db, vid, ScriptedLLM(script(delivery("Deliver within 90 days"))))
    assert rejections(db)["Deliver within 90 days"]["kind"] == "statement"


def test_wrong_bound_keeps_the_requirement_but_drops_the_rule(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    stats = extract_version(db, vid, ScriptedLLM(script(delivery(condition=c("at_most", "capacity.lead_days", bound=300)))))
    r = db.execute("SELECT * FROM requirement").fetchone()
    assert r["condition"] is None and "300 does not appear" in r["condition_note"] and stats["conditions_dropped"] == 1
    assert r["extract_version"] == "extract-v2"
    assert db.execute("SELECT kind FROM requirement_rejection").fetchone()["kind"] == "condition"


def test_lot_is_normalised_or_rejected_but_never_silently_lost(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    extract_version(db, vid, ScriptedLLM(script(delivery(lot="Lot 1"), delivery("Deliver desks (wrong lot) within 30 days", lot="Lot 9"))))
    assert db.execute("SELECT lot_no FROM requirement").fetchone()["lot_no"] == "1"
    bad = rejections(db)["Deliver desks (wrong lot) within 30 days"]
    assert bad["kind"] == "lot" and bad["obligation"] == "mandatory"


def test_garbage_responses_are_recorded_not_fatal(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    def garbage(role, user, schema):
        return {"requirements": ["not an object", {"statement": "no fields"}]}
    stats = extract_version(db, vid, ScriptedLLM(garbage))
    assert stats["rejected"] == 2 and stats["accepted"] == 0
    vid2 = vid
    stats = extract_version(db, vid2, ScriptedLLM(lambda r, u, s: {"oops": 1}), force=True)
    assert stats["rejected"] == 1


def test_a_response_too_long_is_split_and_retried(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    class TooLong:
        def __init__(self):
            self.calls = 0
        def json(self, role, system, user, schema):
            self.calls += 1
            if self.calls == 1:
                raise OutputTooLong("hit max_tokens")
            return JsonResult({"requirements": []}, Usage("claude-opus-5-5", 1, 1))
    llm = TooLong()
    assert extract_version(db, vid, llm)["calls"] >= 2 and llm.calls >= 3


def test_other_llm_errors_roll_back_and_raise(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    def down(role, user, schema):
        raise LLMError("provider unreachable")
    with pytest.raises(LLMError):
        extract_version(db, vid, ScriptedLLM(down))
    assert db.execute("SELECT count(*) n FROM document WHERE extracted_at IS NOT NULL").fetchone()["n"] == 0


def test_decomposed_conditions_are_each_validated(db, tmp_path, monkeypatch):
    vid = setup(db, tmp_path, monkeypatch)
    items = [{"label": "Delivery within 30 days", "mandatory": True, "quote": "deliver all desks within 30 days of order",
              "condition": c("at_most", "capacity.lead_days", bound=30), "evidence_needed": []},
             {"label": "Invented check", "mandatory": True, "quote": "deliver all desks within 30 days of order",
              "condition": c("at_most", "capacity.lead_days", bound=99), "evidence_needed": []},
             {"label": "Quote not in the source", "mandatory": False, "quote": "this sentence does not exist in any document here",
              "condition": None, "evidence_needed": []}]
    extract_version(db, vid, ScriptedLLM(script(delivery(conditions=items))))
    rows = db.execute("SELECT * FROM requirement_condition ORDER BY ordinal").fetchall()
    assert [r["label"] for r in rows] == [i["label"] for i in items]
    assert rows[0]["condition"]["bound"] == 30 and rows[1]["condition"] is None and "99" in rows[1]["condition_note"]
    assert rows[2]["quote"] is None and "not found" in rows[2]["condition_note"]

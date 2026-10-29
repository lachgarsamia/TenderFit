from tenderfit.changes.diff import Change, FieldChange, PassageChange
from tenderfit.changes.impact import Dep, plan_reassessment

DEPS = [
    Dep(1, "source", "passage", "10"),          # delivery -> Spec 3.2
    Dep(1, "fact", "fact", "5"),                # delivery -> lead time
    Dep(2, "source", "passage", "20"),          # comparable contract -> ITT 5.1
    Dep(2, "parameter", "field", "notice.deadline"),
    Dep(3, "source", "passage", "30"),          # BS EN 1729-2 -> Spec 2.3
    Dep(3, "read", "passage", "40"),            # agent opened the clarification log
    Dep(4, "definition", "field", "term:working days"),
]


def test_fig8_v2_changes_delivery_passage_and_deadline():
    ch = Change(fields=[FieldChange("notice.deadline", "2026-10-30", "2026-11-06")],
                passages=[PassageChange("modified", 10, 110, "3.2 Delivery", 97.0, "Spec")], cls="local")
    plan = plan_reassessment(ch, DEPS)
    assert plan.mode == "targeted"
    assert plan.reassess == {1} and plan.rules_only == {2}
    assert plan.reextract == {110}


def test_read_dependency_reopens_requirement_whose_passage_did_not_change():
    ch = Change(passages=[PassageChange("modified", 40, 140, "Clarification log", 95.0, "Log")], cls="local")
    assert plan_reassessment(ch, DEPS).reassess == {3}


def test_fact_change_flows_through_the_same_path():
    assert plan_reassessment(Change(cls="local"), DEPS, facts={5}).reassess == {1}


def test_structural_or_ambiguous_change_means_full_reassessment():
    for cls in ("structural", "ambiguous"):
        plan = plan_reassessment(Change(cls=cls, reasons=["lots added"]), DEPS)
        assert plan.mode == "full" and plan.reason == "lots added"

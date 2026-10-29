import hashlib

from tenderfit.changes.diff import Change, align, classify, field_diff


def P(id, text, section="3.2 Delivery"):
    return {"id": id, "text": text, "section": section, "sha256": hashlib.sha256(text.encode()).hexdigest()}


DELIVERY_V1 = "The supplier shall deliver all desks within 30 days of order. Delivery between 08:00 and 16:00 on Working Days."
DELIVERY_V2 = "The supplier shall deliver all desks within 45 days of order. Delivery between 08:00 and 16:00 on Working Days."
STD = "Desks shall comply with BS EN 1729-2 or an equivalent standard accepted by the University."


def test_field_diff_sees_deadline_and_lot_changes():
    old = {"deadline": "2026-10-30T12:00:00+00:00", "lots": [{"id": "1", "quantity": 200}], "documents": []}
    new = {"deadline": "2026-11-06T12:00:00+00:00", "lots": [{"id": "1", "quantity": 250}, {"id": "2"}], "documents": []}
    paths = {f.path for f in field_diff(old, new)}
    assert paths == {"notice.deadline", "lot.quantity@1", "lots.2"}


def test_alignment_hash_then_fuzzy_within_section():
    out = align([P(1, DELIVERY_V1), P(2, STD, "2.3 Product standard")],
                [P(11, STD, "2.3 Product standard"), P(12, DELIVERY_V2), P(13, "New clause about pallets.", "3.5 Packaging")], "Spec")
    kinds = {(c.kind, c.old_id, c.new_id) for c in out}
    assert ("unchanged", 2, 11) in kinds and ("modified", 1, 12) in kinds and ("added", None, 13) in kinds
    assert next(c for c in out if c.kind == "modified").confidence >= 90


def test_edited_passage_with_high_confidence_is_local():
    ch = Change(passages=align([P(1, DELIVERY_V1)], [P(12, DELIVERY_V2)], "Spec"))
    assert classify(ch, mandatory_passages={1}).cls == "local"


def test_rewritten_mandatory_passage_is_ambiguous():
    ch = Change(passages=align([P(1, DELIVERY_V1)], [P(12, "Goods are to be shipped to site as agreed with the estates team.")], "Spec"))
    c = classify(ch, mandatory_passages={1})
    assert c.cls == "ambiguous" and "mandatory" in c.reasons[0]


def test_lot_split_and_condition_changes_are_structural():
    assert classify(Change(fields=field_diff({"lots": [{"id": "1"}]}, {"lots": [{"id": "1"}, {"id": "2"}]})), set()).cls == "structural"
    ch = Change(passages=align([P(1, "Minimum turnover £1m.", "Notice: conditions of participation")],
                               [P(2, "Minimum turnover £2m.", "Notice: conditions of participation")], "Notice"))
    assert classify(ch, set()).cls == "structural"


def test_essential_document_added_is_structural_and_unreadable_is_ambiguous():
    assert classify(Change(documents=[{"kind": "added", "label": "Appendix C", "essential": True}]), set()).cls == "structural"
    assert classify(Change(documents=[{"kind": "added", "label": "Pricing", "essential": False}],
                           fields=[]), set()).cls == "local"
    assert classify(Change(documents=[{"kind": "unreadable", "label": "Scan.pdf"}]), set()).cls == "ambiguous"


def test_new_version_with_no_difference_is_ambiguous():
    ch = Change(passages=align([P(1, STD)], [P(2, STD)], "Spec"))
    assert classify(ch, set()).cls == "ambiguous"


def test_more_than_30_percent_of_a_document_changed_is_structural():
    old = [P(i, f"Clause {i} text about item number {i} and its rules.", f"{i}. S") for i in range(1, 6)]
    new = [P(10 + i, f"Clause {i} text about item number {i} and its rules." + (" Revised." if i <= 2 else ""), f"{i}. S")
           for i in range(1, 6)]
    assert classify(Change(passages=align(old, new, "Spec")), set()).cls == "structural"

"""Write the fictional Northfield running example (spec Appendix A) as an evaluation dataset."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import northfield  # noqa: E402

root = Path("eval/datasets/northfield")
(root / "releases").mkdir(parents=True, exist_ok=True)
(root / "docs").mkdir(exist_ok=True)
v1 = northfield.release(spec_url="https://portal.test/Spec_Desks_v1.pdf")
v2 = northfield.release(rid="029400-2026", tag="tenderUpdate", date="2026-10-12T18:00:00Z", deadline="2026-11-06T12:00:00Z",
                        spec_url="https://portal.test/Spec_Desks_v2.pdf")
(root / "releases/v1.json").write_text(json.dumps(v1, indent=1))
(root / "releases/v2.json").write_text(json.dumps(v2, indent=1))
(root / "docs/Spec_Desks_v1.pdf").write_bytes(northfield.pdf(northfield.SPEC_V1))
(root / "docs/Spec_Desks_v2.pdf").write_bytes(northfield.pdf(northfield.SPEC_V2))
(root / "docs/ITT.docx").write_bytes(northfield.itt_docx())

A = "Atlas Furniture"
gold = {
    "note": "Fictional running example (spec Appendix A); labels by the authors. Smoke test of the harness, not a result.",
    "today": "2026-10-14",
    "notices": [{
        "id": "northfield",
        "releases": ["releases/v1.json"],
        "documents": {"https://portal.test/Spec_Desks_v1.pdf": "docs/Spec_Desks_v1.pdf"},
        "uploads": [{"ref_label": "Instructions to Tenderers", "file": "docs/ITT.docx"}],
        "confirm_none": ["Appendix B", "Appendix C"],
        "essential_documents": ["Specification", "Instructions to Tenderers"],
        "lots": {"1": {"profiles": {A: {"relevant": True}}}},
        "requirements": [
            {"id": "R1", "lot": "1", "statement": "Deliver all desks within 30 days of order", "quote_contains": "deliver all desks within",
             "obligation": "mandatory", "verdicts": {A: "not_met"}, "verdicts_after": {A: "needs_confirmation"}},
            {"id": "R2", "lot": "1", "statement": "Supply 200 student desks", "quote_contains": "200 student desks",
             "obligation": "mandatory", "verdicts": {A: "met"}, "verdicts_after": {A: "met"}},
            {"id": "R3", "lot": "1", "statement": "Installation included", "quote_contains": "Installation of all desks",
             "obligation": "mandatory", "verdicts": {A: "met"}, "verdicts_after": {A: "met"}},
            {"id": "R4", "lot": "1", "statement": "Desks meet BS EN 1729-2 or an accepted equivalent", "quote_contains": "BS EN 1729-2",
             "obligation": "mandatory", "hard": True, "verdicts": {A: "unknown"}, "verdicts_after": {A: "unknown"}},
            {"id": "R5", "lot": "1", "statement": "Comparable contract (150+ items, last 3 years) with reference",
             "quote_contains": "comparable contract", "obligation": "mandatory", "hard": True,
             "verdicts": {A: "needs_confirmation"}, "verdicts_after": {A: "needs_confirmation"}},
            {"id": "R6", "lot": "1", "statement": "Share of recycled material (scored)", "quote_contains": "recycled material",
             "obligation": "scored", "verdicts": {A: "unknown"}, "verdicts_after": {A: "unknown"}},
        ],
        "amendments": [{"release": "releases/v2.json", "type": "direct + indirect (deadline parameter)",
                        "documents": {"https://portal.test/Spec_Desks_v2.pdf": "docs/Spec_Desks_v2.pdf"},
                        "affected": ["R1", "R5"]}],
    }],
}
(root / "gold.json").write_text(json.dumps(gold, indent=1))
print("wrote", root)

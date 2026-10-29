"""Load the fictional Northfield running example (spec Appendix A) with scripted model responses, for the screens.

Not real model output: every record it creates has an ocid starting with ocds-test- and is labelled fictional."""
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tenderfit.assessment.confirm import confirm  # noqa: E402
from tenderfit.assessment.pipeline import assess_lot  # noqa: E402
from tenderfit.changes.apply import process_pending  # noqa: E402
from tenderfit.db import connect, migrate  # noqa: E402
from tenderfit.documents import service as docs  # noqa: E402
from tenderfit.ingest.client import FtsClient  # noqa: E402
from tenderfit.ingest.importer import run_import  # noqa: E402
from tenderfit.llm import RecordedLLM  # noqa: E402
from tenderfit.profile.seed import seed  # noqa: E402
from tests.fakes import ScriptedLLM  # noqa: E402
from tests.test_eval_run import ask  # noqa: E402
from tests.test_pipeline import verdicts  # noqa: E402

D = Path("eval/datasets/northfield")
FILES = {"https://portal.test/Spec_Desks_v1.pdf": "docs/Spec_Desks_v1.pdf", "https://portal.test/Spec_Desks_v2.pdf": "docs/Spec_Desks_v2.pdf"}


def portal():
    def h(req):
        f = FILES.get(str(req.url))
        if not f:
            return httpx.Response(200, html="<html>Register on the buyer portal to view</html>")
        return httpx.Response(200, content=(D / f).read_bytes(), headers={"content-type": "application/pdf",
                              "content-disposition": f'attachment; filename="{Path(f).name}"'})
    return httpx.Client(transport=httpx.MockTransport(h))


def importer(release):
    def h(req):
        return httpx.Response(200, content=json.dumps({"releases": [release], "links": {}}), headers={"content-type": "application/json"})
    return FtsClient(httpx.Client(transport=httpx.MockTransport(h)), "https://fts.snapshot", sleep=lambda s: None)


RESET = [
    "DELETE FROM agent_step WHERE assessment_id IN (SELECT id FROM assessment WHERE lot_id IN (SELECT id FROM lot WHERE opportunity_id = ANY(%(o)s)))",
    "DELETE FROM change_impact WHERE lot_id IN (SELECT id FROM lot WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM change_event WHERE opportunity_id = ANY(%(o)s)",
    "DELETE FROM verdict_citation WHERE verdict_id IN (SELECT v.id FROM verdict v JOIN assessment a ON a.id = v.assessment_id JOIN lot l ON l.id = a.lot_id WHERE l.opportunity_id = ANY(%(o)s))",
    "DELETE FROM verdict_fact WHERE verdict_id IN (SELECT v.id FROM verdict v JOIN assessment a ON a.id = v.assessment_id JOIN lot l ON l.id = a.lot_id WHERE l.opportunity_id = ANY(%(o)s))",
    "DELETE FROM question WHERE verdict_id IN (SELECT v.id FROM verdict v JOIN assessment a ON a.id = v.assessment_id JOIN lot l ON l.id = a.lot_id WHERE l.opportunity_id = ANY(%(o)s))",
    "DELETE FROM dependency WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM verdict WHERE assessment_id IN (SELECT a.id FROM assessment a JOIN lot l ON l.id = a.lot_id WHERE l.opportunity_id = ANY(%(o)s))",
    "DELETE FROM lot_decision WHERE lot_id IN (SELECT id FROM lot WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM lot_fact_confirmation WHERE lot_id IN (SELECT id FROM lot WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM assessment WHERE lot_id IN (SELECT id FROM lot WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM question WHERE requirement_id IN (SELECT id FROM requirement WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s)))",
    "DELETE FROM requirement_rejection WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s))",
    "UPDATE requirement SET carried_from = NULL WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM requirement WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM passage WHERE document_id IN (SELECT d.id FROM document d JOIN notice_version v ON v.id = d.version_id WHERE v.opportunity_id = ANY(%(o)s))",
    "DELETE FROM document_ref WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM document WHERE version_id IN (SELECT id FROM notice_version WHERE opportunity_id = ANY(%(o)s))",
    "DELETE FROM lot WHERE opportunity_id = ANY(%(o)s)",
    "UPDATE opportunity SET current_version_id = NULL WHERE id = ANY(%(o)s)",
    "DELETE FROM notice_version WHERE opportunity_id = ANY(%(o)s)",
    "DELETE FROM linked_notice WHERE ocid LIKE 'ocds-test-%%'",
    "DELETE FROM raw_release WHERE ocid LIKE 'ocds-test-%%'",
    "DELETE FROM opportunity WHERE id = ANY(%(o)s)",
    "DELETE FROM confirmation WHERE user_name = 'sam'",
]


def reset(c) -> None:
    opps = [r["id"] for r in c.execute("SELECT id FROM opportunity WHERE ocid LIKE 'ocds-test-%%'")]
    for sql in RESET:
        c.execute(sql, {"o": opps})
    c.commit()


def main():
    from datetime import datetime, timezone
    with connect() as c:
        migrate(c)
        if "--reset" in sys.argv:
            reset(c)
        if c.execute("SELECT 1 FROM opportunity WHERE ocid = 'ocds-test-northfield'").fetchone():
            print("demo already loaded")
            return
        ids = seed(c)
        ids = ids or {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM business_profile")}
        llm = RecordedLLM(Path("data/demo_recordings.json"), live=ScriptedLLM(verdicts, ask))
        v1 = json.loads((D / "releases/v1.json").read_text())
        v1["date"] = "2026-10-26T09:00:00Z"
        v1["tender"]["tenderPeriod"]["endDate"] = "2026-11-20T12:00:00Z"
        now = datetime.now(timezone.utc)
        run_import(c, importer(v1), now.replace(year=2026, month=10, day=1), now, now=now)
        opp = c.execute("SELECT id, current_version_id v FROM opportunity WHERE ocid = 'ocds-test-northfield'").fetchone()
        lots = {r["lot_no"]: r["id"] for r in c.execute("SELECT id, lot_no FROM lot WHERE opportunity_id = %s", (opp["id"],))}
        docs.process_version(c, opp["v"], portal())
        assess_lot(c, lots["2"], ids["Pennine Seating"], llm, http=portal(), agent=False)
        itt = next(r for r in docs.inventory(c, opp["v"])["documents"] if r["label"] == "Instructions to Tenderers")
        docs.attach_upload(c, opp["v"], "ITT.docx", (D / "docs/ITT.docx").read_bytes(), "sam", itt["id"])
        for r in docs.inventory(c, opp["v"])["documents"]:
            if r["kind"] == "referenced":
                confirm(c, "G7", "sam", r["id"], "none_issued")
        assess_lot(c, lots["1"], ids["Atlas Furniture"], llm, http=portal(), agent=True)
        confirm(c, "G1", "sam", lots["1"], "pursue", profile_id=ids["Atlas Furniture"])
        v2 = json.loads((D / "releases/v2.json").read_text())
        v2["date"] = "2026-10-28T18:00:00Z"
        v2["tender"]["tenderPeriod"]["endDate"] = "2026-11-27T12:00:00Z"
        run_import(c, importer(v2), now.replace(year=2026, month=10, day=1), now, now=now)
        process_pending(c, llm, http=portal(), agent=True)
        r = json.loads((D / "releases/v1.json").read_text())
        r.update(ocid="ocds-test-riverside", id="030100-2026", date="2026-10-27T10:00:00Z",
                 buyer={"id": "GB-TEST-RIVERSIDE", "name": "Riverside Borough Council"})
        r["tender"].update(title="Civic Centre office furniture refresh", description="Office desks and storage.",
                           tenderPeriod={"endDate": "2026-12-04T17:00:00Z"})
        r["tender"]["lots"] = r["tender"]["lots"][:1]
        r["tender"]["items"] = r["tender"]["items"][:1]
        for d in r["tender"]["documents"]:
            if d["documentType"] == "technicalSpecifications":
                d["url"] = "https://portal.test/Spec_Desks_v2.pdf"
            if d.get("noticeType"):
                d["id"] = r["id"]
        run_import(c, importer(r), now.replace(year=2026, month=10, day=1), now, now=now)
        rlot = c.execute("""SELECT l.id FROM lot l JOIN opportunity o ON o.id = l.opportunity_id
                            WHERE o.ocid = 'ocds-test-riverside'""").fetchone()["id"]
        assess_lot(c, rlot, ids["Atlas Furniture"], llm, http=portal(), agent=False)
        print("demo loaded (fictional): Northfield lots", lots, "Riverside lot", rlot)


if __name__ == "__main__":
    main()

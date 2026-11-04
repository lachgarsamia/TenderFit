"""Load and drive the fictional demonstration through the real pipeline.

  python -m tenderfit.demo load                 fictional suppliers and tender, first assessments
  python -m tenderfit.demo amend                the buyer publishes an amendment; it is processed
  python -m tenderfit.demo amend --fail-first   the first attempt fails, to show the failed state and its retry
  python -m tenderfit.demo reset                remove the fictional tender (real data is untouched)

Add --fast to use a hashing embedder instead of downloading the real embedding and reranking models."""
import argparse
import hashlib
import json
import os
import re
from datetime import date, timedelta

import httpx

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.auth import create_user, grant
from tenderfit.changes.apply import AMENDMENT_LOCK, ChangeProcessingError, _process_pending, shadow_full
from tenderfit.config import settings
from tenderfit.db import connect, migrate
from tenderfit.demo import scenario
from tenderfit.demo.llm import DemoLLM
from tenderfit.documents import service as docs
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import run_import
from tenderfit.profile import service as ps

AMENDMENT = "090002-2026"
SUPPLIERS = ("Brightwell Furniture (fictional)", "Oakline Seating (fictional)")
# Known, public passwords: created only in demo mode, for fictional suppliers. Set DEMO_PASSWORD to choose your own.
DEMO_USERS = {"brightwell@demo.test": SUPPLIERS[0], "oakline@demo.test": SUPPLIERS[1]}
DEMO_PASSWORD = "fictional-demo-only"


class HashEmbedder:
    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 384
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


class OverlapReranker:
    def score(self, query, texts):
        q = set(re.findall(r"[a-z0-9]+", query.lower()))
        return [len(q & set(re.findall(r"[a-z0-9]+", t.lower()))) / (len(q) or 1) for t in texts]


def portal(delivery_days: int = 30, edit=None) -> httpx.Client:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith(".pdf"):
            return httpx.Response(200, content=scenario.spec_pdf(delivery_days, edit), headers={
                "content-type": "application/pdf", "content-disposition": f'attachment; filename="{r.url.path.rsplit("/", 1)[1]}"'})
        return httpx.Response(200, html="<html>Register on the buyer portal to view this document</html>")
    return httpx.Client(transport=httpx.MockTransport(handler))


def importer(release: dict) -> FtsClient:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"releases": [release], "links": {}}), headers={"content-type": "application/json"})
    return FtsClient(httpx.Client(transport=httpx.MockTransport(handler)), "https://fts.snapshot", sleep=lambda s: None)


def _import(conn, release: dict, today: date) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    run_import(conn, importer(release), now - timedelta(days=30), now, advance_watermark=False, now=now)
    # The fictional source was just read. Marking it so leaves real imports, and their watermark, untouched.
    conn.execute("UPDATE opportunity SET last_checked_at = now(), status = 'open', status_reason = 'fictional notice' WHERE ocid = %s", (scenario.OCID,))
    conn.commit()


def add_supported(conn, pid, attribute, label, value, title, lines, quote, valid_until=None, kind=None, **kw) -> int:
    f = ps.add_fact(conn, pid, attribute, label, value, "declared", "fictional document", **kw)
    eid = ps.add_evidence(conn, pid, kind or attribute, f"{label.replace(' ', '_')}.pdf", scenario.evidence_pdf(title, lines), expires_on=valid_until)
    assert ps.link_support(conn, f, eid, 1, quote), f"demo evidence did not verify: {label}"
    return f


def seed_suppliers(conn, today: date) -> dict[str, int]:
    existing = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM business_profile WHERE name = ANY(%s)", (list(SUPPLIERS),))}
    if len(existing) == 2:
        return existing
    bright = ps.upsert_profile(conn, SUPPLIERS[0], ["UKE", "UKD"], "Fictional maker of school desks in Yorkshire and the North West.")
    conf = {"confirmed_at": today - timedelta(days=5)}
    ps.add_fact(conn, bright, "product", "Student desk ND-120", {"name": "Student desk ND-120", "code": "ND-120", "cpv": ["39160000"]}, "declared", "declared", **conf)
    ps.add_fact(conn, bright, "service", "Installation service", {"service": "installation"}, "declared", "declared", **conf)
    ps.add_fact(conn, bright, "region", "Yorkshire & Humber, North West", {"itl": ["UKE", "UKD"]}, "declared", "declared", **conf)
    ps.add_fact(conn, bright, "capacity", "Up to 300 desks within 45 days", {"max_units": 300, "lead_days": 45, "scope": "desks"}, "declared", "operations", **conf)
    ps.add_fact(conn, bright, "experience", "School contract: 180 desks installed", {"description": "School desks", "units": 180,
                "end": (today - timedelta(days=400)).isoformat(), "reference_letter": False}, "declared", "declared")
    ps.add_fact(conn, bright, "test_report", "BS EN 1729-2 test report", {"standard": "BS EN 1729-2", "product": "ND-120"}, "missing", "none on file")
    # Ends a week after the first deadline and before the amended one: the amendment must reopen this verdict.
    end = today + timedelta(days=28)
    add_supported(conn, bright, "insurance", "Public liability policy", {"type": "public_liability", "amount_gbp": 10_000_000},
                  "Insurance schedule (fictional)", ["Insured: Brightwell Furniture Ltd.", "Cover: Public liability, limit of indemnity GBP 10,000,000.",
                                                     f"Period of insurance: to {end.isoformat()}."],
                  "Cover: Public liability, limit of indemnity GBP 10,000,000.", valid_until=end)
    ps.add_fact(conn, bright, "insurance", "Employers liability policy", {"type": "employers_liability", "amount_gbp": 10_000_000}, "declared", "declared",
                valid_until=today + timedelta(days=300))
    cert_end = today + timedelta(days=200)
    add_supported(conn, bright, "certificate", "ISO 14001 certificate", {"standard": "ISO 14001"}, "ISO 14001:2015 certificate (fictional)",
                  ["This certifies that Brightwell Furniture Ltd operates an environmental management system meeting ISO 14001:2015.",
                   f"Valid until {cert_end.isoformat()}."], "operates an environmental management system meeting ISO 14001:2015", valid_until=cert_end)

    oak = ps.upsert_profile(conn, SUPPLIERS[1], ["UKE"], "Fictional seating manufacturer, chairs only, delivering in Yorkshire.")
    ps.add_fact(conn, oak, "product", "Task chair TC-9", {"name": "Task chair TC-9", "code": "TC-9", "cpv": ["39110000"]}, "declared", "declared", **conf)
    ps.add_fact(conn, oak, "region", "Yorkshire & Humber", {"itl": ["UKE"]}, "declared", "declared", **conf)
    ps.add_fact(conn, oak, "capacity", "Up to 500 chairs within 30 days", {"max_units": 500, "lead_days": 30, "scope": "chairs"}, "declared", "operations", **conf)
    add_supported(conn, oak, "insurance", "Public liability policy", {"type": "public_liability", "amount_gbp": 5_000_000}, "Insurance schedule (fictional)",
                  ["Insured: Oakline Seating Ltd.", "Cover: Public liability, limit of indemnity GBP 5,000,000.", f"Period of insurance: to {(today + timedelta(days=300)).isoformat()}."],
                  "Cover: Public liability, limit of indemnity GBP 5,000,000.", valid_until=today + timedelta(days=300))
    ids = {SUPPLIERS[0]: bright, SUPPLIERS[1]: oak}
    password = os.environ.get("DEMO_PASSWORD", DEMO_PASSWORD)
    for email, name in DEMO_USERS.items():
        grant(conn, create_user(conn, email, password), ids[name])
    conn.commit()
    return ids


def _models(a) -> dict:
    return {"emb": HashEmbedder(), "rr": OverlapReranker()} if a.fast else {}


def load(conn, a) -> None:
    if not settings.demo_scripted_llm:
        raise SystemExit("refusing: this creates accounts with a published password. Set DEMO_SCRIPTED_LLM=true, on a database used only for the demo.")
    today = date.today()
    if conn.execute("SELECT 1 FROM opportunity WHERE ocid = %s", (scenario.OCID,)).fetchone():
        print("already loaded; use reset first")
        return
    ids = seed_suppliers(conn, today)
    bright, oak = ids[SUPPLIERS[0]], ids[SUPPLIERS[1]]
    _import(conn, scenario.release(today), today)
    opp = conn.execute("SELECT id, current_version_id v FROM opportunity WHERE ocid = %s", (scenario.OCID,)).fetchone()
    lots = {r["lot_no"]: r["id"] for r in conn.execute("SELECT id, lot_no FROM lot WHERE opportunity_id = %s", (opp["id"],))}
    llm = DemoLLM()
    docs.process_version(conn, opp["v"], portal())
    # The ITT sits behind the buyer's portal. Brightwell uploaded its copy, so it is private to Brightwell.
    itt = next(r for r in docs.inventory(conn, opp["v"])["documents"] if r["label"] == "Instructions to Tenderers")
    docs.attach_upload(conn, opp["v"], "Instructions_to_Tenderers.docx", scenario.itt_docx(), "Brightwell Furniture", itt["id"], bright)
    assess_lot(conn, lots["1"], bright, llm, http=portal(), agent=False, **_models(a))
    assess_lot(conn, lots["2"], bright, llm, http=portal(), agent=False, **_models(a))
    assess_lot(conn, lots["2"], oak, llm, http=portal(), agent=False, **_models(a))
    print(f"loaded (fictional): lots {lots}, suppliers {ids}; sign in as {', '.join(DEMO_USERS)} (see docs/DEMO.md)")


def amend(conn, a) -> None:
    today = date.today()
    # Held from before the import, so a running worker cannot take this amendment first.
    conn.execute("SELECT pg_advisory_lock(%s)", (AMENDMENT_LOCK,))
    conn.commit()
    try:
        if not conn.execute("SELECT 1 FROM notice_version WHERE release_id = %s", (AMENDMENT,)).fetchone():
            # 45 days instead of 30; the deadline moves two weeks, past the public liability policy's end date.
            _import(conn, scenario.release(today, rid=AMENDMENT, tag="tenderUpdate", published_days_ago=1, deadline_in=35,
                                           spec="https://portal.test/harrowgate/Specification_v2.pdf"), today)
        DemoLLM.failing = 1 if a.fail_first else 0
        impacts = _process_pending(conn, DemoLLM(), portal(45), {"agent": False, **_models(a)})
    except ChangeProcessingError as e:
        print(f"{'The first attempt failed, as requested' if a.fail_first else 'Could not finish'}: {e}\n"
              "Press Retry now on the Changes page, or run: python -m tenderfit.demo amend")
        return
    finally:
        conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (AMENDMENT_LOCK,))
        conn.commit()
    print(f"amendment applied: {len(impacts)} impact(s)")
    for iid in impacts:
        print(conn.execute("SELECT explanation FROM change_impact WHERE id = %s", (iid,)).fetchone()["explanation"])
        if a.audit:
            print("full re-check:", shadow_full(conn, iid, DemoLLM(), http=portal(45), agent=False, **_models(a)))


def reset(conn, a) -> None:
    opp = conn.execute("SELECT id FROM opportunity WHERE ocid = %s", (scenario.OCID,)).fetchone()
    if not opp:
        print("nothing to reset")
        return
    o = {"o": opp["id"], "ocid": scenario.OCID}
    versions = "(SELECT id FROM notice_version WHERE opportunity_id = %(o)s)"
    lots = "(SELECT id FROM lot WHERE opportunity_id = %(o)s)"
    assessments = f"(SELECT id FROM assessment WHERE lot_id IN {lots})"
    verdicts = f"(SELECT id FROM verdict WHERE assessment_id IN {assessments})"
    requirements = f"(SELECT id FROM requirement WHERE version_id IN {versions})"
    documents = f"(SELECT id FROM document WHERE version_id IN {versions})"
    passages = f"(SELECT id FROM passage WHERE document_id IN {documents})"
    refs = f"(SELECT id FROM document_ref WHERE version_id IN {versions})"
    for sql in [f"DELETE FROM agent_step WHERE assessment_id IN {assessments}", f"DELETE FROM change_impact WHERE lot_id IN {lots}",
                "DELETE FROM change_event WHERE opportunity_id = %(o)s",
                f"DELETE FROM version_preparation WHERE from_version_id IN {versions}",
                f"DELETE FROM verdict_condition WHERE verdict_id IN {verdicts}", f"DELETE FROM verdict_citation WHERE verdict_id IN {verdicts}",
                f"DELETE FROM verdict_fact WHERE verdict_id IN {verdicts}", f"DELETE FROM question WHERE requirement_id IN {requirements}",
                f"DELETE FROM dependency WHERE requirement_id IN {requirements}", f"DELETE FROM verdict WHERE id IN {verdicts}",
                f"DELETE FROM lot_decision WHERE lot_id IN {lots}", f"DELETE FROM lot_fact_confirmation WHERE lot_id IN {lots}",
                f"DELETE FROM assessment WHERE lot_id IN {lots}", f"DELETE FROM requirement_condition WHERE requirement_id IN {requirements}",
                f"DELETE FROM requirement_rejection WHERE version_id IN {versions}", f"UPDATE requirement SET carried_from = NULL WHERE version_id IN {versions}",
                f"DELETE FROM requirement WHERE version_id IN {versions}", f"DELETE FROM passage_edge WHERE version_id IN {versions}",
                f"DELETE FROM document_ref_state WHERE ref_id IN {refs}", f"DELETE FROM passage WHERE id IN {passages}",
                f"DELETE FROM document_ref WHERE version_id IN {versions}", f"DELETE FROM document WHERE version_id IN {versions}",
                "DELETE FROM lot WHERE opportunity_id = %(o)s", "UPDATE opportunity SET current_version_id = NULL WHERE id = %(o)s",
                "DELETE FROM notice_version WHERE opportunity_id = %(o)s", "DELETE FROM linked_notice WHERE ocid = %(ocid)s",
                "DELETE FROM raw_release WHERE ocid = %(ocid)s", "DELETE FROM opportunity WHERE id = %(o)s"]:
        conn.execute(sql, o)
    conn.execute("""DELETE FROM confirmation WHERE profile_id IN (SELECT id FROM business_profile WHERE name = ANY(%s))""", (list(SUPPLIERS),))
    conn.commit()
    print("fictional tender removed")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m tenderfit.demo")
    p.add_argument("command", choices=["load", "amend", "reset"])
    p.add_argument("--fast", action="store_true", help="hashing embedder instead of the real models")
    p.add_argument("--fail-first", action="store_true", help="make the first amendment attempt fail")
    p.add_argument("--audit", action="store_true", help="also run the full re-check against the targeted result")
    a = p.parse_args()
    with connect() as conn:
        migrate(conn)
        {"load": load, "amend": amend, "reset": reset}[a.command](conn, a)


if __name__ == "__main__":
    main()

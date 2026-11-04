"""Evaluation runner (spec section 6).

  python -m tenderfit.eval run eval/datasets/northfield --configs B0,B1,B2,B3,B4,B5,B5-full [--live]
  python -m tenderfit.eval regression   # synthetic amendment suite, no model calls; not a real-world benchmark

A dataset directory holds gold.json, release snapshots and document files. Model responses are recorded per
dataset and configuration under eval/recordings/, so a run without --live replays them and costs nothing.
"""
import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import psycopg
from rapidfuzz import fuzz

from tenderfit.assessment.config import BASELINES, PipelineConfig
from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.apply import process_pending
from tenderfit.config import settings
from tenderfit.db import connect, migrate, must
from tenderfit.documents import service as docs
from tenderfit.documents.text import normalise, quote_ok
from tenderfit.eval import metrics as m
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import run_import
from tenderfit.llm import LLM, AnthropicLLM, RecordedLLM
from tenderfit.profile.seed import seed
from tenderfit.profile.validity import check
from tenderfit.rules.engine import Fact

EVAL_DB = "tenderfit_eval"


@dataclass
class LotResult:
    notice: str
    lot: str
    profile: str
    items: list[m.Item]
    provisional: bool
    eligibility: str
    final_with_unread: bool
    cost_usd: float
    seconds: float
    citations: int
    citations_ok: int
    stale_unflagged: int
    extracted: int = 0
    matched: int = 0
    evidence: dict = field(default_factory=dict)
    after: list[m.Item] = field(default_factory=list)
    affected_pred: set = field(default_factory=set)
    affected_gold: set = field(default_factory=set)


def _db_url(name: str) -> str:
    base, _ = settings.database_url.rsplit("/", 1)
    return f"{base}/{name}"


def fresh_db() -> str:
    with psycopg.connect(_db_url("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {EVAL_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {EVAL_DB}")
    url = _db_url(EVAL_DB)
    with connect(url) as c:
        migrate(c)
    return url


def _client(releases: list[dict]) -> FtsClient:
    def handler(req):
        return httpx.Response(200, content=json.dumps({"releases": releases, "links": {}}),
                              headers={"content-type": "application/json"})
    return FtsClient(httpx.Client(transport=httpx.MockTransport(handler)), "https://fts.snapshot", sleep=lambda s: None)


def _portal(root: Path, files: dict[str, str]) -> httpx.Client:
    def handler(req):
        path = files.get(str(req.url))
        if not path:
            return httpx.Response(200, html="<html>Register to view documents</html>")
        body = (root / path).read_bytes()
        ctype = "application/pdf" if path.endswith(".pdf") else \
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return httpx.Response(200, content=body, headers={"content-type": ctype,
                                                          "content-disposition": f'attachment; filename="{Path(path).name}"'})
    return httpx.Client(transport=httpx.MockTransport(handler))


def _import(conn, release: dict) -> None:
    t = datetime.fromisoformat(release["date"].replace("Z", "+00:00"))
    run_import(conn, _client([release]), t.replace(hour=0, minute=0), t.replace(hour=23, minute=59),
               now=t.replace(hour=23, minute=59))


def _prepare_documents(conn, version_id: int, root: Path, notice: dict) -> None:
    docs.process_version(conn, version_id, _portal(root, notice.get("documents", {})))
    inv = docs.inventory(conn, version_id)["documents"]
    for up in notice.get("uploads", []):
        ref = next((r for r in inv if r["label"].lower() == up["ref_label"].lower()), None)
        if ref and ref["status"] != "read":
            docs.attach_upload(conn, version_id, Path(up["file"]).name, (root / up["file"]).read_bytes(), "eval",
                               ref["id"])
    for label in notice.get("confirm_none", []):
        conn.execute("""UPDATE document_ref SET status = 'confirmed_none' WHERE version_id = %s AND lower(label) = lower(%s)
                        AND status <> 'read'""", (version_id, label))
    conn.commit()


def match(gold: list[dict], conn, version_id: int, lot_no: str) -> dict[str, int]:
    """Map gold requirement ids to extracted requirement ids by quote or statement."""
    reqs = conn.execute(
        """SELECT r.id, r.statement, r.quote, r.lot_no, p.text FROM requirement r JOIN passage p ON p.id = r.passage_id
           WHERE r.version_id = %s AND (r.lot_no IS NULL OR r.lot_no = %s)""", (version_id, lot_no)).fetchall()
    out, used = {}, set()
    for g in gold:
        if g.get("lot") not in (None, lot_no):
            continue
        key = normalise(g.get("quote_contains", ""))
        # The requirement's own quote, not its whole passage: one extraction must not take credit for its neighbours.
        cand = [r for r in reqs if r["id"] not in used and key and key in normalise(r["quote"] + " " + r["statement"])]
        cand.sort(key=lambda r: -fuzz.token_set_ratio(normalise(g["statement"]), normalise(r["statement"])))
        if not cand:
            cand = sorted((r for r in reqs if r["id"] not in used and
                           fuzz.token_set_ratio(normalise(g["statement"]), normalise(r["statement"])) >= 70),
                          key=lambda r: -fuzz.token_set_ratio(normalise(g["statement"]), normalise(r["statement"])))
        if cand:
            out[g["id"]] = cand[0]["id"]
            used.add(cand[0]["id"])
    return out


def _items(conn, aid: int, gold: list[dict], mapping: dict[str, int], profile: str, verdict_key: str = "verdicts") -> list[m.Item]:
    pred = {r["requirement_id"]: r["override_status"] or r["status"] for r in conn.execute(
        "SELECT requirement_id, status, override_status FROM verdict WHERE assessment_id = %s", (aid,))}
    return [m.Item(g["id"], g["obligation"], g[verdict_key][profile], pred.get(mapping.get(g["id"])), g.get("hard", False))
            for g in gold if profile in g.get(verdict_key, {})]


def _assessment_checks(conn, aid: int, today: date, essential_unread: bool) -> dict:
    a = conn.execute("SELECT * FROM assessment WHERE id = %s", (aid,)).fetchone()
    cites = conn.execute("""SELECT c.quote, p.text FROM verdict_citation c JOIN passage p ON p.id = c.passage_id
                            JOIN verdict v ON v.id = c.verdict_id WHERE v.assessment_id = %s""", (aid,)).fetchall()
    stale = 0
    for r in conn.execute("""SELECT v.status, v.needs_human, f.* FROM verdict v JOIN verdict_fact vf ON vf.verdict_id = v.id
                             JOIN profile_fact f ON f.id = vf.fact_id WHERE v.assessment_id = %s""", (aid,)).fetchall():
        f = Fact(r["id"], r["attribute"], r["label"], r["value"], r["evidence_status"], r["valid_until"], r["confirm_by"])
        v = check(f.view(), today=today, deadline=None)
        if (not v.usable or v.stale) and r["status"] == "met":
            stale += 1
    elig = (a["dimensions"] or {}).get("eligibility", {}).get("state", "")
    final = not a["provisional"] and elig != "blocker_found"
    quality = {r["evidence_quality"]: r["n"] for r in conn.execute(
        "SELECT evidence_quality, count(*) n FROM verdict WHERE assessment_id = %s GROUP BY 1", (aid,))}
    return {"provisional": a["provisional"], "eligibility": elig, "final_with_unread": final and essential_unread,
            "cost": float(a["cost_usd"]), "citations": len(cites), "evidence": quality,
            "citations_ok": sum(quote_ok(c["quote"], c["text"]) for c in cites), "stale": stale}


def run_config(dataset: Path, cfg: PipelineConfig, llm: LLM, today: date, **assess_kw) -> list[LotResult]:
    gold = json.loads((dataset / "gold.json").read_text())
    url = fresh_db()
    settings.document_dir = str(dataset.parent.parent / "work" / dataset.name / "documents")
    results = []
    with connect(url) as conn:
        seed(conn)
        profiles = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM business_profile")}
        for notice in gold["notices"]:
            releases = [json.loads((dataset / r).read_text()) for r in notice["releases"]]
            http = _portal(dataset, {**notice.get("documents", {}),
                                     **{k: v for a in notice.get("amendments", []) for k, v in a.get("documents", {}).items()}})
            _import(conn, releases[0])
            opp = must(conn.execute("SELECT id, current_version_id FROM opportunity WHERE ocid = %s",
                               (releases[0]["ocid"],)).fetchone())
            _prepare_documents(conn, opp["current_version_id"], dataset, notice)
            inv = docs.inventory(conn, opp["current_version_id"])["documents"]
            essential_unread = any(r["status"] not in ("read", "confirmed_none") for r in inv
                                   if r["label"] in notice.get("essential_documents", []))
            for lot_no, lot_gold in notice["lots"].items():
                lot_id = must(conn.execute("SELECT id FROM lot WHERE opportunity_id = %s AND lot_no = %s",
                                      (opp["id"], lot_no)).fetchone())["id"]
                for pname in lot_gold["profiles"]:
                    t0 = time.monotonic()
                    aid = assess_lot(conn, lot_id, profiles[pname], llm, today=today, config=cfg, http=http, **assess_kw)
                    secs = time.monotonic() - t0
                    mapping = match(notice["requirements"], conn, opp["current_version_id"], lot_no)
                    chk = _assessment_checks(conn, aid, today, essential_unread)
                    extracted = must(conn.execute("""SELECT count(*) n FROM requirement WHERE version_id = %s AND (lot_no IS NULL OR lot_no = %s)""",
                                             (opp["current_version_id"], lot_no)).fetchone())["n"]
                    results.append(LotResult(notice["id"], lot_no, pname, _items(conn, aid, notice["requirements"], mapping, pname),
                                             chk["provisional"], chk["eligibility"], chk["final_with_unread"], chk["cost"], secs,
                                             chk["citations"], chk["citations_ok"], chk["stale"], extracted, len(mapping), chk["evidence"]))
            for amend in notice.get("amendments", []):
                if not cfg.name.startswith("B5"):
                    break
                _import(conn, json.loads((dataset / amend["release"]).read_text()))
                new_vid = must(conn.execute("SELECT current_version_id v FROM opportunity WHERE id = %s", (opp["id"],)).fetchone())["v"]
                _prepare_documents(conn, new_vid, dataset, {**notice, "documents": {**notice.get("documents", {}),
                                                                                     **amend.get("documents", {})}})
                for label in amend.get("confirm_none", []):
                    conn.execute("""UPDATE document_ref SET status = 'confirmed_none' WHERE version_id = %s
                                    AND lower(label) = lower(%s) AND status <> 'read'""", (new_vid, label))
                conn.commit()
                if cfg.targeted:
                    process_pending(conn, llm, http=http, today=today, config=cfg, **assess_kw)
                for r in [r for r in results if r.notice == notice["id"]]:
                    lot_id = must(conn.execute("SELECT id FROM lot WHERE opportunity_id = %s AND lot_no = %s",
                                          (opp["id"], r.lot)).fetchone())["id"]
                    if cfg.targeted:
                        aid = must(conn.execute("""SELECT id FROM assessment WHERE lot_id = %s AND profile_id = %s AND NOT superseded
                                              AND NOT shadow ORDER BY id DESC LIMIT 1""", (lot_id, profiles[r.profile])).fetchone())["id"]
                        imp = conn.execute("SELECT affected FROM change_impact WHERE after_assessment_id = %s", (aid,)).fetchone()
                        affected_new = {a["requirement_id"] for a in (imp["affected"] if imp else [])}
                    else:
                        aid = assess_lot(conn, lot_id, profiles[r.profile], llm, today=today, config=cfg, mode="full",
                                         reason="B4-full: every requirement re-assessed", http=http, **assess_kw)
                        affected_new = {v["requirement_id"] for v in conn.execute(
                            "SELECT requirement_id FROM verdict WHERE assessment_id = %s", (aid,))}
                    mapping = match(notice["requirements"], conn, new_vid, r.lot)
                    back = {v: k for k, v in mapping.items()}
                    r.after = _items(conn, aid, notice["requirements"], mapping, r.profile, "verdicts_after")
                    r.affected_pred = {back[x] for x in affected_new if x in back}
                    r.affected_gold = set(amend["affected"])
        usage = must(conn.execute("""SELECT coalesce(sum(cost_usd), 0) cost, coalesce(sum(input_tokens + output_tokens + cache_read_tokens), 0) tokens,
                                       count(*) calls FROM llm_call""").fetchone())
    for r in results:
        r.evidence["__run_total_cost_usd"] = float(usage["cost"])
        r.evidence["__run_tokens"] = int(usage["tokens"])
        r.evidence["__run_model_calls"] = int(usage["calls"])
    return results


def summarise(results: list[LotResult]) -> dict:
    items = [i for r in results for i in r.items]
    hard = [i for i in items if i.hard]
    after = [i for r in results for i in r.after]
    aff = m.set_prf({(r.notice, r.lot, r.profile, x) for r in results for x in r.affected_pred},
                    {(r.notice, r.lot, r.profile, x) for r in results for x in r.affected_gold})
    cites = sum(r.citations for r in results)
    return {
        "lots": len(results),
        "extraction_recall_mandatory": m.extraction_recall(items),
        "extraction_precision": m.safe_div(sum(r.matched for r in results), sum(r.extracted for r in results)),
        "blocker_recall": m.blocker_recall(items),
        "false_blocker_rate": m.false_blocker_rate(items),
        "false_met_rate": m.false_met_rate(items),
        "macro_f1": m.macro_f1(items),
        "macro_f1_hard": m.macro_f1(hard),
        "per_class": m.per_class(items),
        "final_with_essential_unread": sum(r.final_with_unread for r in results),
        "stale_fact_unflagged": sum(r.stale_unflagged for r in results),
        "quote_match_rate": m.safe_div(sum(r.citations_ok for r in results), cites),
        "cost_per_lot_usd": m.safe_div(sum(r.cost_usd for r in results), len(results)),
        "total_cost_usd_including_extraction": results[0].evidence.get("__run_total_cost_usd") if results else None,
        "total_tokens": results[0].evidence.get("__run_tokens") if results else None,
        "model_calls": results[0].evidence.get("__run_model_calls") if results else None,
        "evidence_quality": _quality(results),
        "seconds_per_lot": m.safe_div(sum(r.seconds for r in results), len(results)),
        "amend_verdict_macro_f1": m.macro_f1(after) if after else None,
        "affected_precision": aff["precision"] if after else None,
        "affected_recall": aff["recall"] if after else None,
        "missed_change_rate": 1 - aff["recall"] if after else None,
        # Not measured by this harness: they need labels this dataset does not have. None is not zero.
        "condition_accuracy": None,
        "retrieval_recall_at_5": None,
        "retrieval_ndcg_at_10": None,
        "citation_entailment": None,
    }


def _quality(results: list[LotResult]) -> dict:
    out: dict = {}
    for r in results:
        for k, v in r.evidence.items():
            if k and not k.startswith("__"):
                out[k] = out.get(k, 0) + v
    return out


def compare(a: list[LotResult], b: list[LotResult], metric, subset=lambda i: True) -> dict:
    ia = {(r.notice, i.req, r.lot, r.profile): i for r in a for i in r.items if subset(i)}
    ib = {(r.notice, i.req, r.lot, r.profile): i for r in b for i in r.items if subset(i)}
    keys = sorted(set(ia) & set(ib))
    return m.paired_bootstrap(keys, metric, lambda k: ia[k], lambda k: ib[k], cluster=lambda k: k[0])


def decisions(results: dict[str, list[LotResult]], summary: dict[str, dict]) -> list[str]:
    """Decision rules fixed before any run. A line can say no; that is a result."""
    out = []
    if "B1" in results and "B4" in results:
        d = compare(results["B1"], results["B4"], m.macro_f1, lambda i: i.hard)
        s1, s4 = summary["B1"], summary["B4"]
        keep = (d["diff"] >= 0.05 and s4["blocker_recall"] >= s1["blocker_recall"] - 1e-9
                and not s4["false_blocker_rate"] > s1["false_blocker_rate"] + 1e-9
                and not s4["false_met_rate"] > s1["false_met_rate"] + 1e-9
                and s4["cost_per_lot_usd"] <= 0.50 and s4["seconds_per_lot"] <= 180)
        out.append(f"Keep the agent (B4 over B1): {'yes' if keep else 'no'} — macro-F1 on hard subset "
                   f"{d['diff']:+.3f} [95% CI {d['lo']:+.3f}, {d['hi']:+.3f}]")
    for arm, name in (("B2", "corrective retrieval"), ("B3", "graph retrieval")):
        if "B1" in results and arm in results:
            d = compare(results["B1"], results[arm], m.macro_f1, lambda i: i.hard)
            worse = summary[arm]["false_met_rate"] > summary["B1"]["false_met_rate"] + 1e-9
            out.append(f"Keep {name} ({arm} over B1): {'yes' if d['diff'] >= 0.05 and not worse else 'no'} — macro-F1 on hard subset "
                       f"{d['diff']:+.3f} [95% CI {d['lo']:+.3f}, {d['hi']:+.3f}]")
    if "B5" in results and "B5-full" in results:
        t = {(r.notice, i.req, r.lot, r.profile): i.pred for r in results["B5"] for i in r.after if i.obligation == "mandatory"}
        f = {(r.notice, i.req, r.lot, r.profile): i.pred for r in results["B5-full"] for i in r.after if i.obligation == "mandatory"}
        dis = sorted(k for k in set(t) & set(f) if t[k] != f[k])
        out.append(f"Keep targeted reassessment (B5 over B5-full): {'yes' if not dis else 'no'} — "
                   f"{len(dis)} mandatory disagreement(s){': ' + ', '.join(k[1] for k in dis) if dis else ''}")
    if "B0" in results and "B1" in results:
        d = compare(results["B0"], results["B1"], m.false_blocker_rate)
        out.append(f"False-blocker rate B1 − B0: {d['diff']:+.3f} [95% CI {d['lo']:+.3f}, {d['hi']:+.3f}] "
                   f"(target ≤ 0.05 and at most half of B0's)")
    return out


def run_metadata(dataset: Path, gold: dict, configs: list[str], live: bool, today: date, model: str) -> dict:
    from tenderfit.assessment.prompts import VERDICT_VERSION
    from tenderfit.extraction.prompts import EXTRACT_VERSION
    root = Path(__file__).resolve().parent.parent.parent
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10).stdout.strip()
        except OSError:
            return "unknown"
    return {"git_commit": git("rev-parse", "HEAD"), "git_uncommitted_changes": bool(git("status", "--porcelain")), "model": model,
            "extract_prompt": EXTRACT_VERSION, "verdict_prompt": VERDICT_VERSION, "dataset": dataset.name, "dataset_version": gold.get("version", "unversioned"),
            "dataset_kind": gold.get("kind", "unlabelled"), "configs": {c: asdict(BASELINES[c]) for c in configs}, "assessed_as_of": str(today),
            "run_at": datetime.now(timezone.utc).isoformat(), "live_model": live, "note": gold.get("note", "")}


def report(dataset: Path, summary: dict[str, dict], decision_lines: list[str], note: str) -> str:
    fmt = lambda v: "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))  # noqa: E731
    keys = [k for k, v in next(iter(summary.values())).items() if not isinstance(v, dict)]
    lines = [f"# Evaluation: {dataset.name}", "", note, "", "| Metric | " + " | ".join(summary) + " |",
             "|---|" + "---|" * len(summary)]
    lines += [f"| {k} | " + " | ".join(fmt(summary[c][k]) for c in summary) + " |" for k in keys]
    lines += ["", "## Pre-registered decision rules", ""] + [f"- {d}" for d in decision_lines]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m tenderfit.eval")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("dataset", type=Path)
    r.add_argument("--configs", default="B0,B1,B2,B3,B4,B5,B5-full")
    r.add_argument("--live", action="store_true", help="call the model for requests not yet recorded")
    r.add_argument("--today", type=date.fromisoformat, default=None)
    sub.add_parser("regression", help="synthetic amendment suite comparing full, static and dynamic reassessment; no model calls")
    k = sub.add_parser("kappa", help="Cohen's kappa between two annotators' gold files")
    k.add_argument("a", type=Path)
    k.add_argument("b", type=Path)
    a = p.parse_args()
    if a.cmd == "kappa":
        def labels(path):
            g = json.loads(path.read_text())
            return {(n["id"], r["id"], prof): v for n in g["notices"] for r in n["requirements"]
                    for prof, v in r.get("verdicts", {}).items()}
        la, lb = labels(a.a), labels(a.b)
        keys = sorted(set(la) & set(lb))
        print(f"verdict kappa {m.kappa([la[x] for x in keys], [lb[x] for x in keys]):.3f} over {len(keys)} labels")
        return
    if a.cmd == "regression":
        from tenderfit.eval.regression import main as regression
        regression()
        return
    gold = json.loads((a.dataset / "gold.json").read_text())
    today = a.today or date.fromisoformat(gold.get("today", str(date.today())))
    results, summary = {}, {}
    for name in a.configs.split(","):
        rec = RecordedLLM(Path("eval/recordings") / a.dataset.name / f"{name}.json", live=AnthropicLLM() if a.live else None)
        results[name] = run_config(a.dataset, BASELINES[name], rec, today)
        summary[name] = summarise(results[name])
        print(name, json.dumps(summary[name], default=str))
    out = Path("eval/reports")
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    note = gold.get("note", "")
    (out / f"{a.dataset.name}-{stamp}.md").write_text(report(a.dataset, summary, decisions(results, summary), note))
    meta = run_metadata(a.dataset, gold, a.configs.split(","), a.live, today, settings.llm_model_extract)
    (out / f"{a.dataset.name}-{stamp}.json").write_text(json.dumps(
        {"metadata": meta, "summary": summary, "lots": {k: [asdict(x) for x in v] for k, v in results.items()}}, default=list, indent=1))
    print(f"report: {out / f'{a.dataset.name}-{stamp}.md'}")


if __name__ == "__main__":
    main()

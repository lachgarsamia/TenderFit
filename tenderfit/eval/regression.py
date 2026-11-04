"""Controlled amendment experiment on the fictional tender. Scripted model; synthetic. NOT a real-world benchmark.

Each mutation edits the tender once. Full reassessment is the reference; targeted reassessment is compared with it twice,
using dependencies known statically from extraction, and using what the verdicts actually consulted as well.
It measures whether the bookkeeping reaches what changed, not how well a model reads procurement documents."""
import argparse
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg

from tenderfit.assessment.pipeline import assess_lot
from tenderfit.changes.apply import process_pending
from tenderfit.config import settings
from tenderfit.db import connect, migrate
from tenderfit.demo import __main__ as demo
from tenderfit.demo import scenario
from tenderfit.demo.llm import DemoLLM
from tenderfit.documents.text import normalise
from tenderfit.eval import metrics as m
from tenderfit.eval.run import _db_url

DB = "tenderfit_regression"
KW: dict[str, Any] = dict(agent=False, emb=demo.HashEmbedder(), rr=demo.OverlapReranker())


@dataclass
class Mutation:
    name: str
    kind: str
    delivery_days: int = 30
    deadline_in: int = 21
    edit: Callable[[str], str] | None = None


MUTATIONS = [
    Mutation("edit in the requirement's own clause", "direct", delivery_days=45),
    Mutation("deadline moves past an insurance expiry", "implicit parameter", deadline_in=35),
    Mutation("deadline moves, no fact is affected", "parameter", deadline_in=23),
    Mutation("definition changed in Appendix B", "indirect reference", edit=lambda t: t.replace("Monday to Friday", "Saturday and Sunday")),
    Mutation("insurance amount raised", "direct", edit=lambda t: t.replace("employers liability insurance of at least £5,000,000", "employers liability insurance of at least £20,000,000")),
    Mutation("warranty changed to match the ITT", "resolves a conflict", edit=lambda t: t.replace("warranty of 5 years", "warranty of 3 years")),
    Mutation("unrelated wording changed", "no effect", edit=lambda t: t.replace("invites tenders for teaching-space furniture", "seeks tenders for teaching-space furniture")),
    Mutation("definition changed and deadline moved", "combined", deadline_in=35, edit=lambda t: t.replace("Monday to Friday", "Saturday and Sunday")),
]


def _fresh() -> str:
    with psycopg.connect(_db_url("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {DB}")
    url = _db_url(DB)
    with connect(url) as c:
        migrate(c)
    return url


def _state(conn) -> dict[tuple, tuple]:
    rows = conn.execute(
        """SELECT a.lot_id, a.profile_id, r.statement, v.status, v.carried_over FROM assessment a JOIN verdict v ON v.assessment_id = a.id
           JOIN requirement r ON r.id = v.requirement_id WHERE a.status = 'done' AND NOT a.superseded AND NOT a.shadow""").fetchall()
    return {(r["lot_id"], r["profile_id"], normalise(r["statement"])): (r["status"], r["carried_over"]) for r in rows}


def _before_after(conn) -> dict[tuple, str]:
    return {k: v[0] for k, v in _state(conn).items()}


def run_mutation(mut: Mutation, mode: str, today: date) -> dict:
    """mode: full, static or dynamic."""
    settings.demo_scripted_llm = True
    url = _fresh()
    with connect(url) as conn:
        demo.load(conn, argparse.Namespace(fast=True))
        pairs = conn.execute("SELECT DISTINCT lot_id, profile_id FROM assessment WHERE status = 'done' AND NOT shadow").fetchall()
        before = _before_after(conn)
        demo._import(conn, scenario.release(today, rid=demo.AMENDMENT, tag="tenderUpdate", published_days_ago=1, deadline_in=mut.deadline_in,
                                            spec="https://portal.test/harrowgate/Specification_v2.pdf"), today)
        http = demo.portal(mut.delivery_days, mut.edit)
        llm = DemoLLM()
        if mode == "full":
            for p in pairs:
                assess_lot(conn, p["lot_id"], p["profile_id"], llm, http=http, mode="full", **KW)
        else:
            process_pending(conn, llm, http=http, static_deps=mode == "static", **KW)
        state = _state(conn)
        reassessed = sum(1 for s in state.values() if not s[1]) if mode != "full" else len(state)
    return {"status": {k: v[0] for k, v in state.items()}, "before": before, "reassessed": reassessed, "total": len(state)}


def _norm(state: dict) -> dict:
    return {(k[1], k[2]): v for k, v in state.items()}


def compare(mut: Mutation, oracle: dict, targeted: dict) -> dict:
    o, t = _norm(oracle["status"]), _norm(targeted["status"])
    before = _norm(oracle["before"])
    changed = {k for k, v in o.items() if before.get(k) != v}
    wrong = sorted(k[1] for k in set(o) | set(t) if o.get(k) != t.get(k))
    return {"mutation": mut.name, "kind": mut.kind, "oracle_changed": len(changed), "disagreements": wrong, "reassessed": targeted["reassessed"],
            "total": targeted["total"], "useful_reassessed": len(changed - {k for k in o if o.get(k) != t.get(k)})}


def summarise(rows: list[dict]) -> dict:
    changed = sum(r["oracle_changed"] for r in rows)
    missed = sum(len(r["disagreements"]) for r in rows)
    reassessed = sum(r["reassessed"] for r in rows)
    useful = sum(r["useful_reassessed"] for r in rows)
    total = sum(r["total"] for r in rows)
    return {"mutations": len(rows), "verdicts_compared": total, "verdicts_changed_by_full": changed, "disagreements_with_full": missed,
            "missed_change_rate": m.safe_div(missed, changed) if changed else 0.0, "reassessment_precision": m.safe_div(useful, reassessed) if reassessed else None,
            "share_reassessed": m.safe_div(reassessed, total)}


def main() -> None:
    settings.demo_scripted_llm = True
    today = date.today()
    out: dict = {"kind": "synthetic regression suite; scripted model; not a real-world benchmark", "run_at": datetime.now(timezone.utc).isoformat(),
                 "modes": {}}
    oracle = {mut.name: run_mutation(mut, "full", today) for mut in MUTATIONS}
    for mode in ("static", "dynamic"):
        rows = [compare(mut, oracle[mut.name], run_mutation(mut, mode, today)) for mut in MUTATIONS]
        out["modes"][mode] = {"summary": summarise(rows), "rows": rows}
    lines = ["# Amendment regression suite (synthetic)", "", out["kind"] + ".", "",
             "| Mutation | Kind | Changed by full | Static disagreements | Dynamic disagreements | Static reassessed | Dynamic reassessed |", "|---|---|---|---|---|---|---|"]
    for s, d in zip(out["modes"]["static"]["rows"], out["modes"]["dynamic"]["rows"]):
        lines.append(f"| {s['mutation']} | {s['kind']} | {s['oracle_changed']} | {len(s['disagreements'])} | {len(d['disagreements'])} | "
                     f"{s['reassessed']}/{s['total']} | {d['reassessed']}/{d['total']} |")
    lines += ["", "| Summary | Static | Dynamic |", "|---|---|---|"]
    for k in out["modes"]["static"]["summary"]:
        lines.append(f"| {k} | {out['modes']['static']['summary'][k]} | {out['modes']['dynamic']['summary'][k]} |")
    report = Path(os.environ.get("EVAL_REPORT_DIR", "eval/reports"))
    report.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (report / f"regression-{stamp}.json").write_text(json.dumps(out, indent=1, default=str))
    (report / f"regression-{stamp}.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nreport: {report / f'regression-{stamp}.md'}")

"""Source study (spec section 5.5, Table 25).

  python scripts/source_study.py fetch [--months 12]   pull releases month by month into raw_release (resumable)
  python scripts/source_study.py report [--cpv 391] [--sample 40]
"""
import argparse
import json
import random
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

from tenderfit.db import connect, migrate
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import run_import

ESSENTIAL_TYPES = {"biddingDocuments", "technicalSpecifications", "eligibilityCriteria", "evaluationCriteria",
                   "economicSelectionCriteria", "technicalSelectionCriteria", "contractDraft", "clarifications"}
FILE_TYPES = ("pdf", "msword", "officedocument", "zip", "excel", "spreadsheet", "rtf", "octet-stream")


def month_windows(months: int, now: datetime):
    end = now
    for _ in range(months):
        start = end - timedelta(days=30.5)
        yield start, end
        end = start


def fetch(months: int) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with connect() as conn:
        migrate(conn)
        for start, end in month_windows(months, now):
            done = conn.execute(
                """SELECT 1 FROM import_run WHERE finished_at IS NOT NULL AND error IS NULL
                   AND window_from <= %s AND window_to >= %s LIMIT 1""", (start, end)).fetchone()
            if done:
                print(f"{start:%Y-%m-%d} .. {end:%Y-%m-%d} already imported")
                continue
            stats = run_import(conn, FtsClient(), start, end, advance_watermark=False)
            print(f"{start:%Y-%m-%d} .. {end:%Y-%m-%d} {json.dumps(stats)}", flush=True)


def probe_document(http: httpx.Client, url: str) -> str:
    try:
        with http.stream("GET", url, follow_redirects=True) as r:
            ctype = r.headers.get("content-type", "").lower()
            disp = r.headers.get("content-disposition", "").lower()
            if r.status_code >= 400:
                return f"http_{r.status_code}"
            if "attachment" in disp or any(t in ctype for t in FILE_TYPES):
                return "file"
            return "html_page"
    except httpx.HTTPError as e:
        return f"error_{type(e).__name__}"


def report(cpv: str, sample: int) -> None:
    with connect() as conn:
        span = conn.execute("SELECT min(release_date) lo, max(release_date) hi, count(*) n FROM raw_release").fetchone()
        uk4 = conn.execute(
            """SELECT o.ocid, o.cpv_codes, min(nv.published_at) first_pub, count(*) versions,
                      bool_or(nv.notice_type = 'UK4') is_uk4
               FROM opportunity o JOIN notice_version nv ON nv.ocid = o.ocid
               GROUP BY o.ocid, o.cpv_codes""").fetchall()
        runs = conn.execute("SELECT sum(throttled) t, sum(pages) p, count(*) n FROM import_run").fetchone()
        latency = [r["d"] for r in conn.execute(
            """SELECT extract(epoch FROM rr.fetched_at - rr.release_date) / 60 d FROM raw_release rr
               JOIN import_run ir ON rr.fetched_at BETWEEN ir.started_at AND coalesce(ir.finished_at, now())
               WHERE ir.window_to - ir.window_from < interval '1 day' AND rr.release_date >= ir.window_from""")]

    def in_cpv(row, prefix):
        return any(c.startswith(prefix) for c in row["cpv_codes"])

    print(f"Data: {span['n']} releases, {span['lo']:%Y-%m-%d} .. {span['hi']:%Y-%m-%d}\n")
    for prefix in (cpv, cpv[:2]):
        rows = [r for r in uk4 if r["is_uk4"] and in_cpv(r, prefix)]
        per_month = Counter(r["first_pub"].strftime("%Y-%m") for r in rows)
        months = sorted(per_month)
        median = statistics.median(per_month.values()) if per_month else 0
        edited = sum(r["versions"] >= 2 for r in rows)
        print(f"CPV {prefix}*: {len(rows)} UK4 opportunities; per month median {median} "
              f"(min {min(per_month.values(), default=0)}) [{', '.join(f'{m}:{per_month[m]}' for m in months)}]")
        print(f"  edited (>=2 versions): {edited}")
    legacy = sum(1 for r in uk4 if not r["is_uk4"] and in_cpv(r, cpv))
    print(f"  legacy-regime tender notices in CPV {cpv}*: {legacy}\n")

    with connect() as conn:
        docs = conn.execute(
            """SELECT o.ocid, d->>'documentType' t, d->>'url' url FROM opportunity o
               JOIN notice_version nv ON nv.id = o.current_version_id, jsonb_array_elements(nv.fields->'documents') d
               WHERE nv.notice_type = 'UK4' AND EXISTS (SELECT 1 FROM unnest(o.cpv_codes) c WHERE c LIKE %s)""",
            (f"{cpv}%",)).fetchall()
    by_opp: dict[str, list] = {}
    for d in docs:
        if d["t"] in ESSENTIAL_TYPES and d["url"]:
            by_opp.setdefault(d["ocid"], []).append(d["url"])
    total = len({d["ocid"] for d in docs})
    picked = random.Random(0).sample(sorted(by_opp), min(sample, len(by_opp)))
    outcomes, downloadable = Counter(), 0
    with httpx.Client(timeout=20, headers={"User-Agent": "TenderFit source study"}) as http:
        for ocid in picked:
            results = [probe_document(http, u) for u in dict.fromkeys(by_opp[ocid])]
            outcomes.update(results)
            downloadable += all(r == "file" for r in results)
    print(f"Documents (CPV {cpv}*, UK4): {total} opportunities, {len(by_opp)} list essential-type document links")
    print(f"  probed {len(picked)}: all essential links downloadable without registration: {downloadable}"
          f" ({downloadable / max(len(picked), 1):.0%}); link outcomes {dict(outcomes)}")
    print("  note: opportunities with no essential-type link count as not downloadable "
          f"-> lower bound {downloadable / max(total, 1):.0%} of all\n")

    if latency:
        q = statistics.quantiles(latency, n=20)
        print(f"API delay (fetch - release date, incremental runs): median {statistics.median(latency):.0f} min, "
              f"p95 {q[-1]:.0f} min over {len(latency)} releases")
    else:
        print("API delay: no incremental-run data yet; needs scheduled imports to accumulate")
    print(f"Rate limits: {runs['t'] or 0} throttled responses (429/503) over {runs['p'] or 0} pages in {runs['n']} runs")


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--months", type=int, default=12)
    r = sub.add_parser("report")
    r.add_argument("--cpv", default="391")
    r.add_argument("--sample", type=int, default=40)
    a = p.parse_args()
    fetch(a.months) if a.cmd == "fetch" else report(a.cpv, a.sample)


if __name__ == "__main__":
    main()

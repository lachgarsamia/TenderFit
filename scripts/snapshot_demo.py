"""Capture the read-only demo snapshot from a running demo API (python scripts/snapshot_demo.py [http://localhost:8000/v1])."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Only the fictional accounts `python -m tenderfit.demo load` creates. Keep in step with DEMO_USERS and DEMO_PASSWORD there.
ACCOUNTS = ("brightwell@demo.test", "oakline@demo.test")
PASSWORD = os.environ.get("DEMO_PASSWORD", "fictional-demo-only")


def capture(base: str, email: str) -> tuple[dict, dict]:
    h = httpx.Client(base_url=base, timeout=120, headers={"X-TenderFit": "1"})
    r = h.post("/auth/login", json={"email": email, "password": PASSWORD})
    if r.status_code != 200:
        raise SystemExit(f"could not sign in as {email}: HTTP {r.status_code}. Is this the demo stack?")
    profile = r.json()["profiles"][0]
    snap: dict = {}

    def get(path: str, required: bool = True):
        r = h.get(path)
        if r.status_code == 200:
            snap[path] = r.json()
            return snap[path]
        if required:
            raise SystemExit(f"{path}: HTTP {r.status_code}")
        return None

    res = h.post("/search", json={"q": "", "limit": 40}).json()
    snap["POST /search"] = res
    lots = {l["id"] for o in res["shortlist"] for l in o["lots"]}
    fictional = {l["id"] for o in res["shortlist"] if o["fictional"] for l in o["lots"]}
    for o in res["shortlist"] + res["no_longer_open"]:
        get(f"/tenders/{o['id']}")
    for path in ("/changes", "/profile/facts", "/profile/evidence-impact", "/profile/pursued"):
        get(path)
    for c in snap["/changes"]:
        lots |= {i["lot_id"] for i in c["impacts"]}
    for lot in sorted(lots):
        get(f"/lots/{lot}")
        for d in get(f"/lots/{lot}/documents")["documents"]:
            if lot in fictional and d["kind"] == "notice":
                d["url"] = None  # the fictional notice has no page on the real service
        a = get(f"/lots/{lot}/assessment", required=False)
        if a and a.get("fictional_notice") and a.get("version"):
            a["version"]["source_url"] = None
        if a and a.get("assessment_id"):
            get(f"/lots/{lot}/preparation")
            for req in a["requirements"]:
                for cite in req["citations"]:
                    get(f"/passages/{cite['passage_id']}", required=False)
    return profile, snap


def main(base: str) -> None:
    out_snap: dict = {"__meta": {"captured_at": datetime.now(timezone.utc).isoformat()}, "profiles": [], "suppliers": {}}
    for email in ACCOUNTS:
        profile, snap = capture(base, email)
        out_snap["profiles"].append(profile)
        out_snap["suppliers"][str(profile["id"])] = snap
    out = Path("web/demo/snapshot.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(out_snap, default=str))
    print(f"{sum(len(s) for s in out_snap['suppliers'].values())} responses, {out.stat().st_size // 1024} KB -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/v1")

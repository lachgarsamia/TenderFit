"""Capture the read-only demo snapshot from a running API (python scripts/snapshot_demo.py [http://localhost:8000])."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

REGION = {"UKC": "North East", "UKD": "North West", "UKE": "Yorkshire & Humber", "UKF": "East Midlands", "UKI": "London",
          "UKJ": "South East"}


def main(base: str) -> None:
    h = httpx.Client(base_url=base, timeout=120)
    snap: dict = {"__meta": {"captured_at": datetime.now(timezone.utc).isoformat()}}

    def get(path: str, required: bool = True):
        r = h.get(path)
        if r.status_code == 200:
            snap[path] = r.json()
            return snap[path]
        if required:
            raise SystemExit(f"{path}: HTTP {r.status_code}")
        return None

    profiles = get("/profiles")
    lots: set[int] = set()
    for p in profiles:
        pid = p["id"]
        q = "Furniture tenders we could deliver in " + ", ".join(REGION.get(r, r) for r in p["regions"])
        res = h.post("/search", json={"q": q, "profile_id": pid, "limit": 40}).json()
        snap[f"POST /search?profile_id={pid}"] = res
        lots |= {l["id"] for o in res["shortlist"] for l in o["lots"]}
        for path in (f"/changes?profile_id={pid}", f"/profile/facts?profile_id={pid}", f"/profiles/{pid}/evidence-impact",
                     f"/profiles/{pid}/pursued"):
            get(path)
        for c in snap[f"/changes?profile_id={pid}"]:
            lots |= {i["lot_id"] for i in c["impacts"]}
    for lot in sorted(lots):
        inv = get(f"/lots/{lot}/documents")
        for d in inv["documents"]:
            if d["kind"] == "notice" and d["url"] and "Notice/0" in d["url"] and any(
                    o["fictional"] for k, v in snap.items() if k.startswith("POST /search") for o in v["shortlist"]
                    if any(l["id"] == lot for l in o["lots"])):
                d["url"] = None
        for p in profiles:
            a = get(f"/lots/{lot}/assessment?profile_id={p['id']}", required=False)
            if a and a.get("fictional_notice") and a.get("version"):
                a["version"]["source_url"] = None
            if a:
                get(f"/lots/{lot}/preparation?profile_id={p['id']}")
    out = Path("web/demo/snapshot.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(snap, default=str))
    print(f"{len(snap) - 1} responses, {out.stat().st_size // 1024} KB -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000")

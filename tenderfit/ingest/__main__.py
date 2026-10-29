import argparse
import json
from datetime import datetime, timezone

from tenderfit.db import connect, migrate
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import incremental, replay, run_import


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m tenderfit.ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("window", help="import a fixed updatedFrom/updatedTo window")
    w.add_argument("--from", dest="since", type=_ts, required=True)
    w.add_argument("--to", dest="until", type=_ts, required=True)
    w.add_argument("--advance-watermark", action="store_true")
    inc = sub.add_parser("incremental", help="import since the last watermark (minus overlap)")
    inc.add_argument("--backfill-days", type=int, default=14)
    sub.add_parser("replay", help="rebuild normalised tables from stored raw releases")
    a = p.parse_args()

    with connect() as conn:
        migrate(conn)
        if a.cmd == "window":
            out = run_import(conn, FtsClient(), a.since, a.until, advance_watermark=a.advance_watermark)
        elif a.cmd == "incremental":
            out = incremental(conn, FtsClient(), backfill_days=a.backfill_days)
        else:
            out = replay(conn)
    print(json.dumps(out))


if __name__ == "__main__":
    main()

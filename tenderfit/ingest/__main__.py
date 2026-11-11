import argparse
import json
from datetime import datetime, timezone

from tenderfit.db import connect, migrate
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.importer import ReplayRefused, destructive_rebuild, incremental, rebuild_scope, replay, run_import


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
    r = sub.add_parser("replay", help="re-apply stored raw releases; deletes nothing")
    r.add_argument("--destructive-rebuild", action="store_true",
                   help="DESTRUCTIVE: delete all derived data including every assessment, then rebuild")
    r.add_argument("--confirm-database", help="the exact database name, required with --destructive-rebuild")
    a = p.parse_args()

    with connect() as conn:
        migrate(conn)
        if a.cmd == "window":
            out = run_import(conn, FtsClient(), a.since, a.until, advance_watermark=a.advance_watermark)
        elif a.cmd == "incremental":
            out = incremental(conn, FtsClient(), backfill_days=a.backfill_days)
        elif a.destructive_rebuild:
            print("This would delete:", json.dumps({k: v for k, v in rebuild_scope(conn).items() if v}))
            try:
                out = destructive_rebuild(conn, a.confirm_database)
            except ReplayRefused as e:
                raise SystemExit(str(e))
        else:
            out = replay(conn)
    print(json.dumps(out))


if __name__ == "__main__":
    main()

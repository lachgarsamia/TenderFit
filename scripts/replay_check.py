"""Rebuild normalised tables from raw releases and check the result matches what live imports produced."""
import sys
from datetime import datetime, timezone

from tenderfit.db import connect
from tenderfit.ingest.importer import refresh_statuses, replay

SNAPSHOT = {
    "opportunity": """SELECT o.ocid, o.status, o.deadline, o.title, o.buyer_id, o.cpv_codes, o.regions, nv.release_id
                      FROM opportunity o LEFT JOIN notice_version nv ON nv.id = o.current_version_id ORDER BY o.ocid""",
    "notice_version": "SELECT ocid, release_id, notice_type, release_tag, published_at, content_hash, fields FROM notice_version ORDER BY ocid, release_id",
    "lot": "SELECT o.ocid, l.lot_no, l.title, l.quantity, l.status FROM lot l JOIN opportunity o ON o.id = l.opportunity_id ORDER BY 1, 2",
    "linked_notice": "SELECT ocid, release_id, notice_type, kind, lot_nos, published_at FROM linked_notice ORDER BY ocid, release_id",
    "buyer": "SELECT id, name FROM buyer ORDER BY id",
}


def snapshot(conn) -> dict[str, list]:
    return {k: conn.execute(q).fetchall() for k, q in SNAPSHOT.items()}


def main() -> int:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        refresh_statuses(conn, now)
        conn.commit()
        before = snapshot(conn)
        replay(conn, now=now)
        after = snapshot(conn)
    ok = True
    for table in SNAPSHOT:
        diff = len({repr(r) for r in before[table]} ^ {repr(r) for r in after[table]})
        ok &= diff == 0
        print(f"{table:15} rows {len(before[table]):6} -> {len(after[table]):6}  differing {diff}")
    print("REPLAY OK" if ok else "REPLAY MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

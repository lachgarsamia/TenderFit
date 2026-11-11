"""Rebuild normalised tables from raw releases and check the result matches what live imports produced.

The rebuild happens in a separate temporary database, so the database being checked is only read."""
import sys
from datetime import datetime, timezone

import psycopg

from tenderfit.config import settings
from tenderfit.db import connect, migrate
from tenderfit.ingest.importer import replay

SCRATCH = "tenderfit_replay_check"

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
    base = settings.database_url.rsplit("/", 1)[0]
    with connect() as conn:
        before = snapshot(conn)
        raw = conn.execute("SELECT ocid, release_id, release_date, tags, payload, sha256, source_url, fetched_at FROM raw_release ORDER BY id").fetchall()
    with psycopg.connect(f"{base}/postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {SCRATCH}")
    with connect(f"{base}/{SCRATCH}") as scratch:
        migrate(scratch)
        for r in raw:
            scratch.execute("""INSERT INTO raw_release (ocid, release_id, release_date, tags, payload, sha256, source_url, fetched_at)
                               VALUES (%(ocid)s, %(release_id)s, %(release_date)s, %(tags)s, %(payload)s, %(sha256)s, %(source_url)s, %(fetched_at)s)""", r)
        wm = None
        with connect() as conn:
            wm = conn.execute("SELECT source, watermark FROM import_state").fetchall()
        for w in wm:
            scratch.execute("INSERT INTO import_state (source, watermark) VALUES (%(source)s, %(watermark)s)", w)
        scratch.commit()
        replay(scratch, now=now)
        after = snapshot(scratch)
    with psycopg.connect(f"{base}/postgres", autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")
    ok = True
    for table in SNAPSHOT:
        diff = len({repr(r) for r in before[table]} ^ {repr(r) for r in after[table]})
        ok &= diff == 0
        print(f"{table:15} rows {len(before[table]):6} -> {len(after[table]):6}  differing {diff}")
    print("REPLAY OK" if ok else "REPLAY MISMATCH")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

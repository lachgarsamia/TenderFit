from datetime import datetime, timedelta, timezone

import json

import psycopg
from psycopg.types.json import Jsonb

from tenderfit.config import settings
from tenderfit.ingest import status as st
from tenderfit.ingest.client import FtsClient
from tenderfit.ingest.normalise import Linked, Version, content_hash, finite, normalise, parse_ts

SOURCE = "find_a_tender"


def store_raw(conn: psycopg.Connection, release: dict, source_url: str) -> tuple[int, bool]:
    row = conn.execute(
        """INSERT INTO raw_release (ocid, release_id, release_date, tags, payload, sha256, source_url)
           VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (ocid, release_id) DO NOTHING RETURNING id""",
        (release["ocid"], release["id"], parse_ts(release["date"]), release.get("tag", []),
         json.dumps(release, ensure_ascii=False), content_hash(release), source_url),
    ).fetchone()
    if row:
        return row["id"], True
    existing = conn.execute("SELECT id FROM raw_release WHERE ocid = %s AND release_id = %s",
                            (release["ocid"], release["id"])).fetchone()
    return existing["id"], False


def apply_release(conn: psycopg.Connection, raw_id: int, release: dict, retrieved_at: datetime) -> str | None:
    n = normalise(release)
    if n is None:
        return None
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (n.ocid,))
    if isinstance(n, Linked):
        conn.execute(
            """INSERT INTO linked_notice (ocid, release_id, raw_release_id, notice_type, kind, lot_nos, published_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (ocid, release_id) DO NOTHING""",
            (n.ocid, n.release_id, raw_id, n.notice_type, n.kind, n.lot_nos, n.published_at),
        )
        return n.ocid
    _apply_version(conn, raw_id, n, content_hash(release), retrieved_at)
    return n.ocid


def _apply_version(conn: psycopg.Connection, raw_id: int, v: Version, digest: str, retrieved_at: datetime) -> None:
    if v.buyer and v.buyer.get("id"):
        conn.execute("INSERT INTO buyer (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                     (v.buyer["id"], v.buyer.get("name") or v.buyer["id"]))
    opp = conn.execute(
        "INSERT INTO opportunity (ocid) VALUES (%s) ON CONFLICT (ocid) DO UPDATE SET ocid = EXCLUDED.ocid RETURNING id",
        (v.ocid,),
    ).fetchone()["id"]
    conn.execute(
        """INSERT INTO notice_version (opportunity_id, ocid, release_id, raw_release_id, notice_type, release_tag,
                                       published_at, source_url, retrieved_at, content_hash, fields)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (ocid, release_id) DO NOTHING""",
        (opp, v.ocid, v.release_id, raw_id, v.notice_type, v.release_tag, v.published_at, v.source_url,
         retrieved_at, digest, Jsonb(finite(v.fields))),
    )
    conn.execute(
        """UPDATE buyer SET name = coalesce((SELECT fields->>'buyer_name' FROM notice_version
                                             WHERE fields->>'buyer_id' = buyer.id
                                             ORDER BY published_at DESC, release_id DESC LIMIT 1), name)
           WHERE id = %s""",
        ((v.buyer or {}).get("id"),),
    )
    latest = conn.execute(
        "SELECT id, fields FROM notice_version WHERE ocid = %s ORDER BY published_at DESC, release_id DESC LIMIT 1",
        (v.ocid,),
    ).fetchone()
    f = latest["fields"]
    conn.execute(
        """UPDATE opportunity SET buyer_id = %s, title = %s, legal_basis = %s, deadline = %s, cpv_codes = %s,
                  regions = %s, current_version_id = %s WHERE id = %s""",
        (f.get("buyer_id"), f["title"], f["legal_basis"], f["deadline"], f["cpv"], f["regions"],
         latest["id"], opp),
    )
    for lot in v.fields["lots"]:
        conn.execute("INSERT INTO lot (opportunity_id, lot_no) VALUES (%s, %s) ON CONFLICT DO NOTHING", (opp, lot["id"]))
    for lot in f["lots"]:
        conn.execute("UPDATE lot SET title = %s, quantity = %s WHERE opportunity_id = %s AND lot_no = %s",
                     (lot["title"], lot["quantity"], opp, lot["id"]))


def refresh_statuses(conn: psycopg.Connection, now: datetime, ocids: set[str] | None = None) -> int:
    wm = conn.execute("SELECT watermark FROM import_state WHERE source = %s", (SOURCE,)).fetchone()
    last_checked = wm["watermark"] if wm else None
    fresh_for = timedelta(hours=settings.status_fresh_hours)
    rows = conn.execute(
        """SELECT o.id, o.ocid, o.deadline,
                  coalesce((SELECT array_agg(l.lot_no) FROM lot l WHERE l.opportunity_id = o.id), '{}') AS lot_nos,
                  (SELECT count(*) FROM linked_notice n WHERE n.ocid = o.ocid AND n.kind = 'cancellation') AS cancels,
                  (SELECT coalesce(jsonb_agg(n.lot_nos), '[]') FROM linked_notice n
                    WHERE n.ocid = o.ocid AND n.kind = 'award') AS awards
           FROM opportunity o WHERE %s::text[] IS NULL OR o.ocid = ANY(%s)""",
        (list(ocids) if ocids is not None else None,) * 2,
    ).fetchall()
    for r in rows:
        overall, per_lot = st.derive(now=now, deadline=r["deadline"], last_checked=last_checked, fresh_for=fresh_for,
                                     lot_nos=r["lot_nos"], cancellations=r["cancels"], award_lots=r["awards"])
        conn.execute("UPDATE opportunity SET status = %s, status_reason = %s, last_checked_at = %s WHERE id = %s",
                     (overall.status, overall.reason, last_checked, r["id"]))
        for no, s in per_lot.items():
            conn.execute("UPDATE lot SET status = %s WHERE opportunity_id = %s AND lot_no = %s", (s.status, r["id"], no))
    return len(rows)


def _windows(start: datetime, end: datetime, hours: int):
    while start < end:
        stop = min(start + timedelta(hours=hours), end)
        yield start, stop
        start = stop


def run_import(conn: psycopg.Connection, client: FtsClient, since: datetime, until: datetime,
               advance_watermark: bool = True, now: datetime | None = None) -> dict:
    run_id = conn.execute(
        "INSERT INTO import_run (source, window_from, window_to) VALUES (%s, %s, %s) RETURNING id",
        (SOURCE, since, until),
    ).fetchone()["id"]
    conn.commit()
    stats = {"pages": 0, "seen": 0, "new": 0}
    touched: set[str] = set()
    try:
        for a, b in _windows(since, until, settings.import_chunk_hours):
            for url, package in client.release_pages(a, b):
                retrieved_at = datetime.now(timezone.utc)
                with conn.transaction():
                    for release in package.get("releases", []):
                        raw_id, is_new = store_raw(conn, release, url)
                        stats["seen"] += 1
                        stats["new"] += is_new
                        if is_new and (ocid := apply_release(conn, raw_id, release, retrieved_at)):
                            touched.add(ocid)
                    stats["pages"] += 1
                    conn.execute("UPDATE import_run SET pages = %s, releases_seen = %s, releases_new = %s, throttled = %s WHERE id = %s",
                                 (stats["pages"], stats["seen"], stats["new"], client.throttled, run_id))
                conn.commit()
        with conn.transaction():
            if advance_watermark:
                conn.execute(
                    """INSERT INTO import_state (source, watermark) VALUES (%s, %s)
                       ON CONFLICT (source) DO UPDATE SET watermark = greatest(import_state.watermark, EXCLUDED.watermark)""",
                    (SOURCE, until),
                )
            stats["statuses"] = refresh_statuses(conn, now or datetime.now(timezone.utc))
            conn.execute("UPDATE import_run SET finished_at = now() WHERE id = %s", (run_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.execute("UPDATE import_run SET error = %s, finished_at = now() WHERE id = %s", (str(e)[:2000], run_id))
        conn.commit()
        raise
    stats["touched"] = len(touched)
    stats["throttled"] = client.throttled
    return stats


def incremental(conn: psycopg.Connection, client: FtsClient, now: datetime | None = None,
                backfill_days: int = 14) -> dict:
    now = now or datetime.now(timezone.utc)
    wm = conn.execute("SELECT watermark FROM import_state WHERE source = %s", (SOURCE,)).fetchone()
    since = (wm["watermark"] - timedelta(minutes=settings.import_overlap_minutes)) if wm else now - timedelta(days=backfill_days)
    return run_import(conn, client, since, now, now=now)


def replay(conn: psycopg.Connection, now: datetime | None = None) -> dict:
    with conn.transaction():
        conn.execute("TRUNCATE lot, linked_notice, notice_version, opportunity, buyer RESTART IDENTITY CASCADE")
        applied = 0
        for r in conn.execute("SELECT id, payload, fetched_at FROM raw_release ORDER BY release_date, id").fetchall():
            applied += apply_release(conn, r["id"], json.loads(r["payload"]), r["fetched_at"]) is not None
        refresh_statuses(conn, now or datetime.now(timezone.utc))
    conn.commit()
    return {"applied": applied}

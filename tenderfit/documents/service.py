import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg

from tenderfit.config import settings
from tenderfit.documents import passages as ps
from tenderfit.documents import references
from tenderfit.documents.fetch import Fetched, fetch, sniff_type
from tenderfit.documents.inventory import NAMED_TO_TYPE, RefState, coverage, is_essential, notice_blocks
from tenderfit.documents.parse import parse


def _store_bytes(content: bytes, sha: str, ext: str) -> str:
    root = Path(settings.document_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sha}{ext}"
    if not path.exists():
        path.write_bytes(content)
    return str(path)


def _insert_passages(conn: psycopg.Connection, doc_id: int, items: list[ps.Passage]) -> int:
    for p in items:
        conn.execute(
            "INSERT INTO passage (document_id, ordinal, page, section, text, sha256, kind) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (doc_id, p.ordinal, p.page, p.section, p.text, p.sha256, p.kind),
        )
    return len(items)


def ensure_inventory(conn: psycopg.Connection, version_id: int) -> None:
    if conn.execute("SELECT 1 FROM document_ref WHERE version_id = %s LIMIT 1", (version_id,)).fetchone():
        return
    v = conn.execute(
        """SELECT nv.fields, nv.source_url, rr.payload FROM notice_version nv
           JOIN raw_release rr ON rr.id = nv.raw_release_id WHERE nv.id = %s""", (version_id,)).fetchone()
    release = json.loads(v["payload"])
    doc_id = conn.execute(
        """INSERT INTO document (version_id, url, origin, access, filename, retrieved_at, note)
           VALUES (%s, %s, 'source', 'read', 'Tender notice', now(), 'notice fields') RETURNING id""",
        (version_id, v["source_url"]),
    ).fetchone()["id"]
    items = ps.build(notice_blocks(release))
    for p in items:
        p.kind = "field"
    _insert_passages(conn, doc_id, items)
    conn.execute(
        """INSERT INTO document_ref (version_id, label, kind, status, url, document_id, essential, note)
           VALUES (%s, 'Tender notice', 'notice', 'read', %s, %s, true, 'fields and lots')""",
        (version_id, v["source_url"], doc_id),
    )
    seen = set()
    for d in v["fields"]["documents"]:
        if d.get("noticeType"):
            continue
        key = (d.get("documentType"), d.get("url"), d.get("title"))
        if key in seen:
            continue
        seen.add(key)
        label = d.get("title") or (d.get("documentType") or "document")
        conn.execute(
            """INSERT INTO document_ref (version_id, label, ocds_type, url, referenced_in, kind, status, essential,
                                         source_doc_id, note)
               VALUES (%s, %s, %s, %s, 'Notice: documents', 'document', 'not_retrieved', %s, %s, %s)""",
            (version_id, label, d.get("documentType"), d.get("url"), is_essential(d.get("documentType"), d.get("title")),
             d.get("id"), None if d.get("url") else "no link in the notice"),
        )


def _record_document(conn: psycopg.Connection, version_id: int, f: Fetched, url: str | None, origin: str,
                     uploaded_by: str | None = None) -> tuple[int, str, str | None]:
    ext = sniff_type(f.content, f.content_type or "", f.filename)
    parsed = parse(f.content, ext or "")
    access = "read" if not parsed.unreadable else "unreadable"
    note = parsed.unreadable or f"{len(parsed.blocks)} blocks"
    doc_id = conn.execute(
        """INSERT INTO document (version_id, url, sha256, origin, access, retrieved_at, filename, content_type,
                                 page_count, note, storage_path, uploaded_by)
           VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s, %s) RETURNING id""",
        (version_id, url, f.sha256, origin, access, f.filename, f.content_type, parsed.page_count, note,
         _store_bytes(f.content, f.sha256, ext or ".bin"), uploaded_by),
    ).fetchone()["id"]
    if access == "read":
        items = ps.build(parsed.blocks)
        _insert_passages(conn, doc_id, items)
        _add_referenced(conn, version_id, doc_id, items)
    return doc_id, access, parsed.unreadable


def _add_referenced(conn: psycopg.Connection, version_id: int, doc_id: int, items: list[ps.Passage]) -> None:
    existing = conn.execute(
        """SELECT r.label, r.ocds_type, d.filename FROM document_ref r LEFT JOIN document d ON d.id = r.document_id
           WHERE r.version_id = %s""", (version_id,)).fetchall()
    headings = {r["section"].lower() for r in conn.execute(
        """SELECT DISTINCT p.section FROM passage p JOIN document d ON d.id = p.document_id
           WHERE d.version_id = %s AND p.section IS NOT NULL""", (version_id,))}
    names = " ".join(f"{r['label']} {r['filename'] or ''}".lower() for r in existing)
    types = {r["ocds_type"] for r in existing}
    for p in items:
        for ref in references.find(p.text):
            low = ref.label.lower()
            if ref.kind == "section":
                continue
            if low in names or any(h.startswith(low) for h in headings) or (NAMED_TO_TYPE.get(low) or "-") in types:
                continue
            where = f"{p.section or 'document'}" + (f" · p.{p.page}" if p.page else "")
            conn.execute(
                """INSERT INTO document_ref (version_id, label, kind, status, referenced_in, essential, note)
                   VALUES (%s, %s, 'referenced', 'not_retrieved', %s, %s, 'mentioned in a document; not found in the pack')""",
                (version_id, ref.label, where, is_essential(None, ref.label)),
            )
            names += " " + low


def inherit_from_previous(conn: psycopg.Connection, version_id: int) -> int:
    """User uploads and 'buyer issued none' confirmations carry to the next version of the same notice."""
    prev = conn.execute(
        """SELECT p.id FROM notice_version v JOIN notice_version p ON p.ocid = v.ocid
           WHERE v.id = %s AND (p.published_at, p.release_id) < (v.published_at, v.release_id)
           ORDER BY p.published_at DESC, p.release_id DESC LIMIT 1""", (version_id,)).fetchone()
    if not prev:
        return 0
    n = 0
    old = conn.execute("""SELECT r.*, d.origin FROM document_ref r LEFT JOIN document d ON d.id = r.document_id
                          WHERE r.version_id = %s AND (d.origin = 'upload' OR r.status = 'confirmed_none')""",
                       (prev["id"],)).fetchall()
    for o in old:
        cur = conn.execute("""SELECT * FROM document_ref WHERE version_id = %s AND lower(label) = lower(%s)
                              AND kind = %s AND status = 'not_retrieved'""", (version_id, o["label"], o["kind"])).fetchone()
        if o["kind"] == "referenced" and not cur:
            continue
        if o["status"] == "confirmed_none":
            if cur:
                conn.execute("UPDATE document_ref SET status = 'confirmed_none', note = %s WHERE id = %s",
                             (f"{o['note']} (carried from previous version)", cur["id"]))
                n += 1
            continue
        doc = conn.execute(
            """INSERT INTO document (version_id, url, sha256, origin, access, retrieved_at, filename, content_type, page_count,
                                     note, storage_path, uploaded_by)
               SELECT %s, url, sha256, origin, access, retrieved_at, filename, content_type, page_count,
                      'carried from previous version', storage_path, uploaded_by FROM document WHERE id = %s RETURNING id""",
            (version_id, o["document_id"])).fetchone()["id"]
        conn.execute("""INSERT INTO passage (document_id, ordinal, page, section, text, sha256, kind, embedding)
                        SELECT %s, ordinal, page, section, text, sha256, kind, embedding FROM passage WHERE document_id = %s""",
                     (doc, o["document_id"]))
        if cur:
            conn.execute("UPDATE document_ref SET status = %s, document_id = %s, note = 'uploaded earlier; carried over' WHERE id = %s",
                         (o["status"], doc, cur["id"]))
        else:
            conn.execute(
                """INSERT INTO document_ref (version_id, label, ocds_type, kind, status, document_id, essential, referenced_in, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'uploaded earlier; carried over')""",
                (version_id, o["label"], o["ocds_type"], o["kind"], o["status"], doc, o["essential"], o["referenced_in"]))
        n += 1
    return n


def process_version(conn: psycopg.Connection, version_id: int, http: httpx.Client | None = None) -> dict:
    ensure_inventory(conn, version_id)
    out = {"read": 0, "unreadable": 0, "not_retrieved": 0}
    refs = conn.execute(
        "SELECT * FROM document_ref WHERE version_id = %s AND kind = 'document' AND status = 'not_retrieved' AND url IS NOT NULL",
        (version_id,)).fetchall()
    cache: dict[str, tuple[int, str, str | None]] = {}
    for r in refs:
        if r["url"] not in cache:
            f = fetch(r["url"], http)
            if f.outcome == "file":
                cache[r["url"]] = _record_document(conn, version_id, f, r["url"], "source")
            else:
                cache[r["url"]] = (None, "not_retrieved", f.note)
        doc_id, access, note = cache[r["url"]]
        status = {"read": "read", "unreadable": "unreadable"}.get(access, "not_retrieved")
        out[status] += 1
        conn.execute("UPDATE document_ref SET status = %s, document_id = %s, note = %s WHERE id = %s",
                     (status, doc_id, note, r["id"]))
    if not conn.execute("SELECT 1 FROM document WHERE version_id = %s AND note = 'carried from previous version' LIMIT 1",
                        (version_id,)).fetchone():
        out["inherited"] = inherit_from_previous(conn, version_id)
    conn.commit()
    return out


def attach_upload(conn: psycopg.Connection, version_id: int, filename: str, content: bytes, user: str,
                  ref_id: int | None = None) -> dict:
    ensure_inventory(conn, version_id)
    f = Fetched("file", "uploaded", content, filename, None)
    doc_id, access, note = _record_document(conn, version_id, f, None, "upload", user)
    status = "read" if access == "read" else "unreadable"
    if ref_id:
        conn.execute("UPDATE document_ref SET status = %s, document_id = %s, note = %s WHERE id = %s AND version_id = %s",
                     (status, doc_id, note or f"uploaded by {user}", ref_id, version_id))
    else:
        ref_id = conn.execute(
            """INSERT INTO document_ref (version_id, label, kind, status, document_id, essential, referenced_in, note)
               VALUES (%s, %s, 'document', %s, %s, %s, 'uploaded by user', %s) RETURNING id""",
            (version_id, filename, status, doc_id, is_essential(None, filename), note or f"uploaded by {user}"),
        ).fetchone()["id"]
    conn.commit()
    return {"document_id": doc_id, "ref_id": ref_id, "status": status, "note": note}


def inventory(conn: psycopg.Connection, version_id: int) -> dict:
    refs = conn.execute(
        """SELECT r.*, d.filename, d.origin, (SELECT count(*) FROM passage p WHERE p.document_id = r.document_id) passages
           FROM document_ref r LEFT JOIN document d ON d.id = r.document_id WHERE r.version_id = %s ORDER BY r.kind DESC, r.id""",
        (version_id,)).fetchall()
    cov = coverage([RefState(r["kind"], r["essential"], r["status"]) for r in refs])
    essential = [r for r in refs if r["essential"] and r["kind"] != "notice"]
    return {"coverage": cov, "essential_read": sum(r["status"] == "read" for r in essential),
            "essential_total": len(essential), "documents": refs,
            "checked_at": datetime.now(timezone.utc)}

import hashlib
import io
from datetime import date
from pathlib import Path

import docx
import psycopg
import pymupdf
from psycopg.types.json import Jsonb

from tenderfit.config import settings
from tenderfit.documents.text import quote_score, QUOTE_THRESHOLD
from tenderfit.profile.validity import FactView, daily_status, default_confirm_by


def upsert_profile(conn: psycopg.Connection, name: str, regions: list[str], description: str,
                   is_fictional: bool = True) -> int:
    return conn.execute(
        """INSERT INTO business_profile (name, regions, description, is_fictional) VALUES (%s, %s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET regions = EXCLUDED.regions, description = EXCLUDED.description
           RETURNING id""", (name, regions, description, is_fictional)).fetchone()["id"]


def add_fact(conn: psycopg.Connection, profile_id: int, attribute: str, label: str, value: dict,
             evidence_status: str, source: str, confirmed_at: date | None = None, valid_until: date | None = None,
             confirm_by: date | None = None) -> int:
    return conn.execute(
        """INSERT INTO profile_fact (profile_id, attribute, label, value, evidence_status, source, confirmed_at,
                                     valid_until, confirm_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (profile_id, attribute, label, Jsonb(value), evidence_status, source, confirmed_at, valid_until,
         confirm_by or default_confirm_by(attribute, confirmed_at)),
    ).fetchone()["id"]


def update_fact(conn: psycopg.Connection, fact_id: int, **changes) -> list[int]:
    allowed = {"value", "evidence_status", "confirmed_at", "valid_until", "confirm_by", "label"}
    sets = {k: (Jsonb(v) if k == "value" else v) for k, v in changes.items() if k in allowed}
    if "confirmed_at" in sets and "confirm_by" not in sets:
        attr = conn.execute("SELECT attribute FROM profile_fact WHERE id = %s", (fact_id,)).fetchone()["attribute"]
        sets["confirm_by"] = default_confirm_by(attr, sets["confirmed_at"])
    if sets:
        cols = ", ".join(f"{k} = %s" for k in sets)
        conn.execute(f"UPDATE profile_fact SET {cols}, needs_update_reason = NULL, updated_at = now() WHERE id = %s",
                     (*sets.values(), fact_id))
    return verdicts_using(conn, [fact_id])


def _pages(content: bytes, filename: str) -> list[str]:
    if content[:5] == b"%PDF-":
        return [p.get_text() for p in pymupdf.open(stream=content, filetype="pdf")]
    if filename.lower().endswith(".docx"):
        return ["\n".join(p.text for p in docx.Document(io.BytesIO(content)).paragraphs)]
    return [content.decode("utf-8", "replace")]


def add_evidence(conn: psycopg.Connection, profile_id: int, kind: str, filename: str, content: bytes,
                 issuer: str | None = None, issued_on: date | None = None, expires_on: date | None = None) -> int:
    sha = hashlib.sha256(content).hexdigest()
    root = Path(settings.document_dir) / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sha}{Path(filename).suffix}"
    path.write_bytes(content)
    pages = _pages(content, filename)
    eid = conn.execute(
        """INSERT INTO evidence_file (profile_id, kind, issuer, issued_on, expires_on, sha256, filename, storage_path, page_count)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (profile_id, kind, issuer, issued_on, expires_on, sha, filename, str(path), len(pages)),
    ).fetchone()["id"]
    for i, text in enumerate(pages, start=1):
        conn.execute("INSERT INTO evidence_page (evidence_file_id, page, text) VALUES (%s, %s, %s)", (eid, i, text))
    return eid


def link_support(conn: psycopg.Connection, fact_id: int, evidence_file_id: int, page: int, quote: str) -> bool:
    row = conn.execute("SELECT text FROM evidence_page WHERE evidence_file_id = %s AND page = %s",
                       (evidence_file_id, page)).fetchone()
    score = quote_score(quote, row["text"]) if row else 0.0
    ok = score >= QUOTE_THRESHOLD
    conn.execute(
        "INSERT INTO fact_support (fact_id, evidence_file_id, page, quote, verified, score) VALUES (%s, %s, %s, %s, %s, %s)",
        (fact_id, evidence_file_id, page, quote, ok, score))
    if ok:
        ef = conn.execute("SELECT expires_on FROM evidence_file WHERE id = %s", (evidence_file_id,)).fetchone()
        conn.execute(
            """UPDATE profile_fact SET evidence_status = CASE WHEN evidence_status IN ('declared', 'missing', 'unknown')
                                                              THEN 'document_on_file' ELSE evidence_status END,
                      valid_until = coalesce(valid_until, %s) WHERE id = %s""", (ef["expires_on"], fact_id))
    return ok


def facts(conn: psycopg.Connection, profile_id: int) -> list[dict]:
    return conn.execute(
        """SELECT f.*, coalesce(jsonb_agg(jsonb_build_object('evidence_file_id', s.evidence_file_id, 'filename', e.filename,
                                          'page', s.page, 'quote', s.quote, 'verified', s.verified))
                                FILTER (WHERE s.id IS NOT NULL), '[]') AS supports
           FROM profile_fact f LEFT JOIN fact_support s ON s.fact_id = f.id LEFT JOIN evidence_file e ON e.id = s.evidence_file_id
           WHERE f.profile_id = %s GROUP BY f.id ORDER BY f.attribute, f.id""", (profile_id,)).fetchall()


def verdicts_using(conn: psycopg.Connection, fact_ids: list[int]) -> list[int]:
    return [r["verdict_id"] for r in conn.execute(
        """SELECT DISTINCT vf.verdict_id FROM verdict_fact vf JOIN verdict v ON v.id = vf.verdict_id
           JOIN assessment a ON a.id = v.assessment_id WHERE vf.fact_id = ANY(%s) AND NOT a.superseded""", (fact_ids,))]


def daily_check(conn: psycopg.Connection, today: date) -> dict:
    stale = []
    for f in conn.execute("SELECT * FROM profile_fact WHERE evidence_status <> 'needs_update'").fetchall():
        reason = daily_status(FactView(f["attribute"], f["evidence_status"], f["valid_until"], f["confirm_by"],
                                       f["confirmed_at"]), today)
        if reason:
            conn.execute("UPDATE profile_fact SET evidence_status = 'needs_update', needs_update_reason = %s, "
                         "updated_at = now() WHERE id = %s", (reason, f["id"]))
            stale.append(f["id"])
    verdict_ids = verdicts_using(conn, stale) if stale else []
    lots = conn.execute(
        """SELECT DISTINCT a.lot_id, a.profile_id FROM verdict v JOIN assessment a ON a.id = v.assessment_id
           WHERE v.id = ANY(%s)""", (verdict_ids,)).fetchall()
    conn.commit()
    return {"stale_facts": stale, "verdicts_to_recheck": verdict_ids, "lots": [dict(l) for l in lots]}

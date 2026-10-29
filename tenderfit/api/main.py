from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query
from pydantic import BaseModel

from tenderfit.config import settings
from tenderfit.db import connect, migrate
from tenderfit.ingest import status as st


@asynccontextmanager
async def lifespan(app: FastAPI):
    with connect() as conn:
        migrate(conn)
    yield


app = FastAPI(title="TenderFit", lifespan=lifespan)

from tenderfit.api.assessments import router  # noqa: E402
from tenderfit.api.screens import router as screens_router  # noqa: E402

app.include_router(router)
app.include_router(screens_router)


class LotOut(BaseModel):
    lot_no: str
    title: str | None
    status: str


class OpportunityOut(BaseModel):
    id: int
    ocid: str
    title: str | None
    buyer: str | None
    deadline: datetime | None
    status: str
    status_reason: str | None
    last_checked_at: datetime | None
    versions: int
    current_release_id: str | None
    notice_url: str | None
    amended: bool
    cpv_codes: list[str]
    regions: list[str]
    lots: list[LotOut]


def effective_status(stored: str, reason: str | None, deadline: datetime | None, last_checked: datetime | None,
                     now: datetime) -> st.Status:
    if stored in (st.CANCELLED, st.AWARDED):
        return st.Status(stored, reason or "")
    return st.lot_status(now=now, deadline=deadline, last_checked=last_checked,
                         fresh_for=timedelta(hours=settings.status_fresh_hours), cancelled=False, awarded=False)


@app.get("/health")
def health():
    with connect() as conn:
        wm = conn.execute("SELECT watermark FROM import_state").fetchone()
        last = conn.execute("SELECT * FROM import_run ORDER BY id DESC LIMIT 1").fetchone()
    return {"watermark": wm and wm["watermark"], "last_run": last}


@app.get("/opportunities", response_model=list[OpportunityOut])
def opportunities(cpv: list[str] = Query(default=[]), region: list[str] = Query(default=[]),
                  open_only: bool = True, q: str | None = None, limit: int = Query(50, le=200), offset: int = 0):
    now = datetime.now(timezone.utc)
    where, args = ["TRUE"], []
    if cpv:
        where.append("EXISTS (SELECT 1 FROM unnest(o.cpv_codes) c WHERE c LIKE ANY(%s))")
        args.append([f"{c}%" for c in cpv])
    if region:
        where.append("EXISTS (SELECT 1 FROM unnest(o.regions) r WHERE r LIKE ANY(%s))")
        args.append([f"{r}%" for r in region])
    if q:
        where.append("o.title ILIKE %s")
        args.append(f"%{q}%")
    if open_only:
        where.append("o.status IN ('open', 'unverified') AND (o.deadline IS NULL OR o.deadline > now())")
    sql = f"""
        SELECT o.*, b.name AS buyer, nv.release_id AS current_release_id, nv.source_url AS notice_url,
               (SELECT count(*) FROM notice_version v WHERE v.ocid = o.ocid) AS versions,
               coalesce((SELECT jsonb_agg(jsonb_build_object('lot_no', l.lot_no, 'title', l.title, 'status', l.status)
                                          ORDER BY l.lot_no) FROM lot l WHERE l.opportunity_id = o.id), '[]') AS lots
        FROM opportunity o
        LEFT JOIN buyer b ON b.id = o.buyer_id
        LEFT JOIN notice_version nv ON nv.id = o.current_version_id
        WHERE {' AND '.join(where)}
        ORDER BY o.deadline NULLS LAST, o.id LIMIT %s OFFSET %s"""
    with connect() as conn:
        rows = conn.execute(sql, (*args, limit, offset)).fetchall()
    out = []
    for r in rows:
        s = effective_status(r["status"], r["status_reason"], r["deadline"], r["last_checked_at"], now)
        out.append(OpportunityOut(**{**r, "status": s.status, "status_reason": s.reason, "amended": r["versions"] > 1}))
    return out

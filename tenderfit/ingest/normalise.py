import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
TENDER_TAGS = {"tender", "tenderUpdate", "tenderAmendment"}
AWARD_NOTICE_TYPES = {"UK6", "UK7"}


@dataclass
class Version:
    ocid: str
    release_id: str
    notice_type: str
    release_tag: str
    published_at: datetime
    source_url: str | None
    buyer: dict | None
    fields: dict


@dataclass
class Linked:
    ocid: str
    release_id: str
    notice_type: str
    kind: str
    published_at: datetime
    lot_nos: list[str] | None = field(default=None)


def content_hash(release: dict) -> str:
    return hashlib.sha256(json.dumps(release, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time(23, 59, 59), LONDON).astimezone(timezone.utc)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LONDON)
    return dt.astimezone(timezone.utc)


def notice_docs(release: dict) -> list[dict]:
    docs = list(release.get("tender", {}).get("documents", []))
    for a in release.get("awards", []):
        docs += a.get("documents", [])
    return [d for d in docs if d.get("noticeType")]


def notice_type(release: dict) -> str | None:
    own = [d for d in notice_docs(release) if d.get("id") == release.get("id")]
    docs = own or notice_docs(release)
    return docs[0]["noticeType"] if docs else None


def _tender_tag(release: dict) -> str | None:
    tags = [t for t in release.get("tag", []) if t in TENDER_TAGS]
    return tags[0] if tags else None


def _cpv(tender: dict) -> list[str]:
    codes = []
    if (c := tender.get("classification")) and c.get("scheme") == "CPV":
        codes.append(c["id"])
    for item in tender.get("items", []):
        for c in [item.get("classification") or {}] + item.get("additionalClassifications", []):
            if c.get("scheme") == "CPV" and c.get("id"):
                codes.append(c["id"])
    return sorted(set(codes))


def _regions(tender: dict) -> list[str]:
    return sorted({a["region"] for i in tender.get("items", []) for a in i.get("deliveryAddresses", []) if a.get("region")})


def _lots(tender: dict) -> list[dict]:
    lots = tender.get("lots") or [{"id": "1"}]
    out = []
    for lot in lots:
        items = [i for i in tender.get("items", []) if i.get("relatedLot", lot["id"]) == lot["id"]]
        qty = [i["quantity"] for i in items if isinstance(i.get("quantity"), (int, float))]
        out.append({
            "id": str(lot["id"]),
            "title": lot.get("title"),
            "description": lot.get("description"),
            "status": lot.get("status"),
            "value": lot.get("value"),
            "quantity": sum(qty) if qty else None,
            "contract_period": lot.get("contractPeriod"),
            "cpv": sorted({c["id"] for i in items for c in i.get("additionalClassifications", []) if c.get("scheme") == "CPV"}),
            "regions": sorted({a["region"] for i in items for a in i.get("deliveryAddresses", []) if a.get("region")}),
        })
    return out


def normalise(release: dict) -> Version | Linked | None:
    ocid, rid = release["ocid"], release["id"]
    published = parse_ts(release["date"])
    nt = notice_type(release)
    tags = set(release.get("tag", []))

    if "tenderCancellation" in tags or nt == "UK12":
        return Linked(ocid, rid, nt or "legacy", "cancellation", published)
    if tags & {"award", "awardUpdate"} and (nt in AWARD_NOTICE_TYPES or nt is None):
        lots = sorted({str(l) for a in release.get("awards", []) for l in a.get("relatedLots", [])}) or None
        return Linked(ocid, rid, nt or "legacy", "award", published, lots)

    tag = _tender_tag(release)
    if not tag or nt not in ("UK4", None):
        return None
    tender = release.get("tender", {})
    deadline_raw = tender.get("tenderPeriod", {}).get("endDate")
    own = next((d for d in notice_docs(release) if d.get("id") == rid), None)
    fields = {
        "title": tender.get("title"),
        "buyer_id": (release.get("buyer") or {}).get("id"),
        "description": tender.get("description"),
        "source_status": tender.get("status"),
        "legal_basis": tender.get("legalBasis", {}).get("id"),
        "procurement_method": tender.get("procurementMethodDetails") or tender.get("procurementMethod"),
        "deadline_raw": deadline_raw,
        "deadline": parse_ts(deadline_raw).isoformat() if deadline_raw else None,
        "value": tender.get("value"),
        "cpv": _cpv(tender),
        "regions": _regions(tender),
        "lots": _lots(tender),
        "documents": [
            {k: d.get(k) for k in ("id", "documentType", "noticeType", "title", "url", "datePublished")}
            for d in tender.get("documents", [])
        ],
        "submission_url": tender.get("submissionMethodDetails"),
    }
    return Version(ocid, rid, nt or "legacy", tag, published, own.get("url") if own else None, release.get("buyer"), fields)

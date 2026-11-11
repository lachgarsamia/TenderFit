"""What can be checked about a supplier's evidence file without a person: only what its text layer actually shows.

Levels, each adding to the last: exists (we hold the file), relevant (it names what the fact claims), supports (the cited
quote is in it and nothing contradicts the fact), current (it has not expired). 'Verified by a person' is stored separately."""
import re
from datetime import date

from tenderfit.documents.text import normalise
from tenderfit.extraction.validate import numbers_in
from tenderfit.rules.engine import INSURANCE_TYPES, insurance_type_in

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                      "october", "november", "december"], start=1)}
DATE_TEXT = r"(\d{1,2}) (" + "|".join(MONTHS) + r") (\d{4})|(\d{4})-(\d{2})-(\d{2})"
EXPIRY = re.compile(r"(?:valid until|expires?(?: on)?|expiry(?: date)?:?|until|\bto\b)\s+(?:" + DATE_TEXT + ")", re.I)
LEGAL = re.compile(r"\b(?:ltd|limited|plc|llp)\b\.?", re.I)


def parse_expiry(text: str) -> date | None:
    """The latest date introduced by expiry wording, or None when the text gives none."""
    found = []
    for m in EXPIRY.finditer(text):
        try:
            if m.group(1):
                found.append(date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))))
            else:
                found.append(date(int(m.group(4)), int(m.group(5)), int(m.group(6))))
        except ValueError:
            continue
    return max(found) if found else None


def verify_fact_evidence(fact: dict, profile_name: str, pages: list[str], quote_ok: bool, today: date, stated_expiry: date | None) -> dict:
    """Returns {'level', 'checks': {name: {'result', 'detail'}}, 'mismatch': bool}. Results: ok, mismatch, not_found, not_applicable."""
    text = "\n".join(pages)
    if not normalise(text):
        return {"level": "exists", "mismatch": False, "checks": {"contents": {
            "result": "not_inspected", "detail": "The file has no readable text, so its contents could not be checked."}}}
    norm, checks, value = normalise(text), {}, fact.get("value") or {}
    name = normalise(LEGAL.sub("", profile_name))
    checks["supplier"] = {"result": "ok" if name and name in norm else "not_found",
                          "detail": f"Names {profile_name}" if name in norm else f"Does not name {profile_name}"}
    standard = value.get("standard")
    if standard:
        hit = normalise(str(standard)) in norm
        checks["standard"] = {"result": "ok" if hit else "mismatch", "detail": f"{'Names' if hit else 'Does not name'} {standard}"}
    if fact.get("attribute") == "insurance":
        wanted = insurance_type_in(str(value.get("type") or fact.get("label")))
        named = {k for k, words in INSURANCE_TYPES.items() if any(w.replace("_", " ") in text.lower() for w in words)}
        if wanted:
            checks["policy_type"] = {"result": "ok" if wanted in named else ("mismatch" if named else "not_found"),
                                     "detail": f"Names {', '.join(sorted(named)) or 'no policy type'}; the fact claims {wanted}"}
        amount = value.get("amount_gbp")
        if isinstance(amount, (int, float)):
            hit = float(amount) in numbers_in(text)
            checks["amount"] = {"result": "ok" if hit else "not_found", "detail": f"£{amount:,.0f} {'found' if hit else 'not found'} in the file"}
    product = value.get("product") or value.get("code")
    if product:
        hit = normalise(str(product)) in norm
        checks["product"] = {"result": "ok" if hit else "not_found", "detail": f"{product} {'named' if hit else 'not named'} in the file"}
    expiry = parse_expiry(text)
    if expiry:
        conflict = bool(stated_expiry and stated_expiry != expiry)
        checks["expiry"] = {"result": "mismatch" if conflict else "ok", "date": expiry.isoformat(),
                            "detail": f"The file says {expiry.isoformat()}" + (f", not {stated_expiry} as entered" if conflict else "")}
    else:
        checks["expiry"] = {"result": "not_found", "detail": "No expiry date recognised in the file"}
    checks["quote"] = {"result": "ok" if quote_ok else "not_found",
                       "detail": "The quoted sentence is in the file" if quote_ok else "The quoted sentence was not found on that page"}
    mismatch = any(c["result"] == "mismatch" for c in checks.values())
    relevant = any(checks.get(k, {}).get("result") == "ok" for k in ("standard", "policy_type", "product", "amount")) or quote_ok
    effective = stated_expiry or expiry or fact.get("valid_until")
    if mismatch or not relevant:
        level = "relevant" if relevant else "exists"
    elif not quote_ok:
        level = "relevant"
    elif effective and effective < today:
        level = "supports"
        checks["current"] = {"result": "mismatch", "detail": f"Expired on {effective.isoformat()}"}
    else:
        level = "current"
    return {"level": level, "checks": checks, "mismatch": mismatch, "expiry": expiry}

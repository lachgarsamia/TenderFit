from dataclasses import dataclass
from datetime import date, timedelta

TIME_SENSITIVE = {"capacity", "workload", "staff", "installation_slots"}
DECLARED_YEARLY = {"region", "product", "registration", "service"}
CONFIRM_DAYS = {"capacity": 30, "workload": 30, "staff": 30, "installation_slots": 30}
RECONFIRM_DAYS = 365


@dataclass(frozen=True)
class FactView:
    attribute: str
    evidence_status: str
    valid_until: date | None
    confirm_by: date | None
    confirmed_at: date | None


@dataclass(frozen=True)
class Validity:
    usable: bool
    stale: bool
    reason: str | None
    question: str | None = None


def default_confirm_by(attribute: str, confirmed_at: date | None) -> date | None:
    if confirmed_at is None:
        return None
    if attribute in CONFIRM_DAYS:
        return confirmed_at + timedelta(days=CONFIRM_DAYS[attribute])
    if attribute in DECLARED_YEARLY:
        return confirmed_at + timedelta(days=RECONFIRM_DAYS)
    return None


def daily_status(f: FactView, today: date) -> str | None:
    if f.evidence_status in ("missing", "unknown"):
        return None
    if f.valid_until and f.valid_until < today:
        return f"expired on {f.valid_until.isoformat()}"
    if f.confirm_by and f.confirm_by < today:
        return f"confirmation due by {f.confirm_by.isoformat()}"
    return None


def check(f: FactView, *, today: date, deadline: date | None, contract_start: date | None = None,
          cover_required_for_contract: bool = False) -> Validity:
    """Validity of a fact for one requirement, measured against the lot's deadline (Table 12)."""
    if f.evidence_status in ("missing", "unknown"):
        return Validity(False, False, f"evidence {f.evidence_status}")
    stale = daily_status(f, today)
    if f.evidence_status == "needs_update" or stale:
        return Validity(False, True, stale or "needs update")
    at = deadline or today
    if f.valid_until and f.valid_until < at:
        if f.attribute == "insurance":
            return Validity(False, True, f"policy ends {f.valid_until.isoformat()}, before the submission deadline",
                            "Will the policy be renewed before the deadline? Upload the renewed schedule.")
        return Validity(False, True, f"expires {f.valid_until.isoformat()}, before the submission deadline",
                        "Upload the renewed certificate.")
    if (f.attribute == "insurance" and cover_required_for_contract and contract_start and f.valid_until
            and f.valid_until < contract_start):
        return Validity(True, True, f"policy ends {f.valid_until.isoformat()}, before contract start",
                        "Will the policy be renewed before the contract starts?")
    if f.confirm_by and f.confirm_by < at and f.attribute in TIME_SENSITIVE:
        return Validity(True, True, f"confirmation due {f.confirm_by.isoformat()}, before the deadline")
    return Validity(True, False, None)

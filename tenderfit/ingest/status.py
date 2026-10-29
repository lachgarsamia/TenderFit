from dataclasses import dataclass
from datetime import datetime, timedelta

OPEN, CLOSED, CANCELLED, AWARDED, UNVERIFIED = "open", "closed", "cancelled", "awarded", "unverified"
SHORTLIST = {OPEN, UNVERIFIED}


@dataclass(frozen=True)
class Status:
    status: str
    reason: str


def lot_status(*, now: datetime, deadline: datetime | None, last_checked: datetime | None,
               fresh_for: timedelta, cancelled: bool, awarded: bool) -> Status:
    if cancelled:
        return Status(CANCELLED, "UK12 procurement termination notice published")
    if awarded:
        return Status(AWARDED, "UK6 contract award notice published for this lot")
    if deadline is not None and deadline <= now:
        return Status(CLOSED, "submission deadline has passed")
    if last_checked is None or now - last_checked > fresh_for:
        return Status(UNVERIFIED, "source not checked recently")
    if deadline is None:
        return Status(UNVERIFIED, "no submission deadline in the notice")
    return Status(OPEN, "tender notice with future deadline, checked recently")


_RANK = [OPEN, UNVERIFIED, CLOSED, AWARDED, CANCELLED]


def opportunity_status(lots: list[Status]) -> Status:
    if not lots:
        return Status(UNVERIFIED, "no lots")
    return min(lots, key=lambda s: _RANK.index(s.status))


def derive(*, now: datetime, deadline: datetime | None, last_checked: datetime | None, fresh_for: timedelta,
           lot_nos: list[str], cancellations: int, award_lots: list[list[str] | None]) -> tuple[Status, dict[str, Status]]:
    awarded_all = any(a is None for a in award_lots)
    awarded = {l for a in award_lots if a for l in a}
    per_lot = {
        no: lot_status(now=now, deadline=deadline, last_checked=last_checked, fresh_for=fresh_for,
                       cancelled=cancellations > 0, awarded=awarded_all or no in awarded)
        for no in lot_nos
    }
    return opportunity_status(list(per_lot.values())), per_lot

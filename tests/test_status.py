from datetime import datetime, timedelta, timezone

from tenderfit.ingest import status as st

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=timezone.utc)
FRESH = timedelta(hours=24)
FUTURE = NOW + timedelta(days=20)
PAST = NOW - timedelta(minutes=1)


def lot(**kw):
    base = dict(now=NOW, deadline=FUTURE, last_checked=NOW - timedelta(hours=1), fresh_for=FRESH,
                cancelled=False, awarded=False)
    return st.lot_status(**(base | kw)).status


def test_open_when_future_deadline_and_checked_recently():
    assert lot() == st.OPEN


def test_cancellation_wins_over_everything():
    assert lot(cancelled=True, awarded=True, deadline=PAST) == st.CANCELLED


def test_award_wins_over_deadline():
    assert lot(awarded=True, deadline=PAST) == st.AWARDED


def test_closed_once_deadline_passed_even_if_stale():
    assert lot(deadline=PAST, last_checked=None) == st.CLOSED


def test_deadline_exactly_now_is_closed():
    assert lot(deadline=NOW) == st.CLOSED


def test_stale_check_is_unverified_not_open():
    assert lot(last_checked=NOW - timedelta(hours=25)) == st.UNVERIFIED
    assert lot(last_checked=None) == st.UNVERIFIED


def test_missing_deadline_is_unverified():
    assert lot(deadline=None) == st.UNVERIFIED


def test_award_for_one_lot_leaves_other_lot_open():
    overall, per_lot = st.derive(now=NOW, deadline=FUTURE, last_checked=NOW, fresh_for=FRESH,
                                 lot_nos=["1", "2"], cancellations=0, award_lots=[["1"]])
    assert per_lot["1"].status == st.AWARDED and per_lot["2"].status == st.OPEN
    assert overall.status == st.OPEN


def test_award_without_lot_ids_applies_to_all_lots():
    overall, _ = st.derive(now=NOW, deadline=FUTURE, last_checked=NOW, fresh_for=FRESH,
                           lot_nos=["1", "2"], cancellations=0, award_lots=[None])
    assert overall.status == st.AWARDED


def test_cancellation_applies_to_every_lot():
    overall, per_lot = st.derive(now=NOW, deadline=FUTURE, last_checked=NOW, fresh_for=FRESH,
                                 lot_nos=["1", "2"], cancellations=1, award_lots=[])
    assert overall.status == st.CANCELLED and {s.status for s in per_lot.values()} == {st.CANCELLED}

from datetime import datetime, timezone

from tenderfit.ingest.normalise import Linked, Version, normalise, parse_ts
from tests.factories import uk4, uk6, uk12


def test_uk4_becomes_version_with_cpv_from_items():
    v = normalise(uk4())
    assert isinstance(v, Version)
    assert v.notice_type == "UK4" and v.release_tag == "tender"
    assert v.fields["cpv"] == ["39160000"] and v.fields["regions"] == ["UKE"]
    assert v.source_url.endswith("/Notice/000100-2026")


def test_tender_update_is_a_version():
    v = normalise(uk4(rid="000150-2026", tag="tenderUpdate"))
    assert isinstance(v, Version) and v.release_tag == "tenderUpdate"


def test_uk12_is_cancellation_even_though_tender_status_is_complete():
    n = normalise(uk12())
    assert isinstance(n, Linked) and n.kind == "cancellation" and n.notice_type == "UK12"


def test_uk6_is_award_with_lots():
    n = normalise(uk6(lots=("2",)))
    assert isinstance(n, Linked) and n.kind == "award" and n.lot_nos == ["2"]


def test_planning_and_uk5_are_skipped():
    assert normalise({"ocid": "o", "id": "1", "date": "2026-10-01T00:00:00Z", "tag": ["planning"]}) is None
    uk5 = uk6()
    uk5["awards"][0]["documents"][0]["noticeType"] = "UK5"
    assert normalise(uk5) is None


def test_date_only_deadline_is_end_of_day_london():
    assert parse_ts("2026-11-06") == datetime(2026, 11, 6, 23, 59, 59, tzinfo=timezone.utc)
    assert parse_ts("2026-07-06") == datetime(2026, 7, 6, 22, 59, 59, tzinfo=timezone.utc)


def test_offset_deadline_is_converted_to_utc():
    assert parse_ts("2027-09-30T12:00:00+01:00") == datetime(2027, 9, 30, 11, 0, tzinfo=timezone.utc)


def test_release_without_lots_gets_one_implicit_lot():
    r = uk4()
    del r["tender"]["lots"]
    assert [l["id"] for l in normalise(r).fields["lots"]] == ["1"]

from datetime import datetime, timezone

import httpx
import pytest

from tenderfit.ingest.client import FtsClient, SourceError, retry_after_seconds

BASE = "https://fts.test/api/1.0"
T0, T1 = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 2, tzinfo=timezone.utc)


def client(handler, **kw):
    sleeps = []
    c = FtsClient(httpx.Client(transport=httpx.MockTransport(handler)), BASE, sleep=sleeps.append, **kw)
    return c, sleeps


def test_follows_next_links_until_absent():
    def handler(req):
        if "cursor" not in req.url.params:
            assert req.url.params["updatedFrom"] == "2026-10-01T00:00:00"
            assert "stages" not in req.url.params
            return httpx.Response(200, json={"releases": [{"id": "a"}], "links": {"next": f"{BASE}/ocdsReleasePackages?cursor=Mg=="}})
        return httpx.Response(200, json={"releases": [{"id": "b"}], "links": {}})

    c, _ = client(handler)
    assert [r["id"] for _, p in c.release_pages(T0, T1) for r in p["releases"]] == ["a", "b"]


def test_waits_retry_after_on_429_and_503():
    calls = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                  httpx.Response(503, headers={"Retry-After": "3"}),
                  httpx.Response(200, json={"releases": []})])
    c, sleeps = client(lambda req: next(calls))
    list(c.release_pages(T0, T1))
    assert sleeps == [7.0, 3.0] and c.throttled == 2


def test_gives_up_after_max_retries():
    c, sleeps = client(lambda req: httpx.Response(429), max_retries=2)
    with pytest.raises(SourceError):
        list(c.release_pages(T0, T1))
    assert len(sleeps) == 2


def test_4xx_fails_fast():
    c, sleeps = client(lambda req: httpx.Response(400))
    with pytest.raises(SourceError):
        list(c.release_pages(T0, T1))
    assert sleeps == []


def test_retry_after_parsing():
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds(None, default=5) == 5
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert retry_after_seconds("garbage", default=9) == 9

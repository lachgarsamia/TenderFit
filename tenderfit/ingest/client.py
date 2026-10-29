import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from tenderfit.config import settings

RETRY_STATUSES = {429, 503}
FMT = "%Y-%m-%dT%H:%M:%S"


class SourceError(Exception):
    pass


def retry_after_seconds(value: str | None, default: float = 60) -> float:
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class FtsClient:
    def __init__(self, http: httpx.Client | None = None, base_url: str | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int | None = None):
        self.http = http or httpx.Client(timeout=60, headers={"User-Agent": "TenderFit/0.0.1"})
        self.base_url = (base_url or settings.fts_base_url).rstrip("/")
        self.sleep = sleep
        self.max_retries = settings.http_max_retries if max_retries is None else max_retries
        self.throttled = 0

    def get_json(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(self.max_retries + 1):
            try:
                r = self.http.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise SourceError(f"transport error: {e}") from e
                self.sleep(min(2 ** attempt, 60))
                continue
            if r.status_code in RETRY_STATUSES:
                self.throttled += 1
                if attempt == self.max_retries:
                    raise SourceError(f"HTTP {r.status_code} after {attempt + 1} attempts")
                self.sleep(retry_after_seconds(r.headers.get("Retry-After")))
                continue
            if r.status_code >= 500 and attempt < self.max_retries:
                self.sleep(min(2 ** attempt, 60))
                continue
            if r.status_code >= 400:
                raise SourceError(f"HTTP {r.status_code} for {r.url}")
            return r.json()
        raise SourceError("unreachable")

    def release_pages(self, updated_from: datetime, updated_to: datetime,
                      stages: str | None = None) -> Iterator[tuple[str, dict]]:
        url = f"{self.base_url}/ocdsReleasePackages"
        params = {"limit": 100, "updatedFrom": _fmt(updated_from), "updatedTo": _fmt(updated_to)}
        if stages:
            params["stages"] = stages
        while url:
            package = self.get_json(url, params)
            yield url if params is None else str(httpx.URL(url, params=params)), package
            url, params = package.get("links", {}).get("next"), None

    def releases_for_ocid(self, ocid: str) -> dict:
        return self.get_json(f"{self.base_url}/ocdsReleasePackages/{ocid}")


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)

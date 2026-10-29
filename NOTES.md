# Notes

## Decisions
- Package modules are added as each release needs them (R0: ingest, jobs, api).
- Migrations are plain numbered SQL files applied in order by a tiny runner; no ORM.

## Live API findings (probed 2026-10-29, window 2026-10-27)
- No key, no auth. 200 in ~0.5s per 100-release page. No 429/503 seen yet; Retry-After handling untested live.
- `links.next` is a full URL with an opaque base64 `cursor`; absent on the last page. Releases are not sorted by date.
- `stages=tender` (the documented default) returned 9 releases/day, all legacy-regime (`legalBasis 32014L0024`).
  Unfiltered returned 483/day, of which 74 `tender`+UK4, 23 `tenderUpdate`+UK4, 4 `tenderCancellation`+UK12.
  The stage filter drops Procurement Act 2023 notices.
- Notice type is not a release field: it sits in a document with `documentType` `tenderNotice`/`awardNotice`/
  `tenderCancellationNotice` and `noticeType` `UK4`/`UK6`/`UK12` (in `tender.documents` or `awards[].documents`).
- Edit = same ocid, new release `id` (= FTS notice id, e.g. `101772-2026`), tag `tenderUpdate`. `amendments` is null,
  so changes must come from diffs, as the spec says.
- PA2023 notices: `tender.classification` is null; CPV is in `items[].additionalClassifications`; regions are ITL codes
  in `items[].deliveryAddresses[].region`. `tenderPeriod.endDate` is sometimes date-only.
- Some releases are cross-posts from Public Contracts Scotland (`links[].rel=canonical` to another host).

## Deviations from the spec
- Importer fetches all stages and filters during normalisation instead of `stages=tender`, because the stage filter
  drops UK4 notices and UK6/UK12 are needed for status anyway (Table 20). Raw releases are all stored.

## Open questions
- Does `stages=planning,tender,award` include PA2023 notices, or only the unfiltered call? Check in source study.

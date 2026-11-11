# Known limitations

## Authentication and authorization

Implemented and tested: accounts, sessions, per-supplier membership, supplier taken from the session at every route and job, and direct and indirect isolation tests. **This has not had an independent security review or penetration test, so it should not be described as production-ready.**

Not implemented:
- **Password reset or change, email verification, multi-factor sign-in.** An operator can only create accounts and grant access.
- **Removing a member or disabling an account has no command or screen.** The columns are there and honoured; changing them needs SQL.
- **`owner` and `member` roles behave the same.** Any member can do everything for that supplier.
- **Rate limiting covers sign-in only.** Uploads and assessment requests can use up model budget up to the per-lot caps.
- **No log of who signed in.** Confirmations and overrides do record the account.

## Remaining security limitations

- Compose provides no TLS. Documents and evidence are unencrypted on a local volume.
- Tender documents and uploads pre-dating migration 014 stay public, as their owner is unknown.
- `npm audit`, production dependency: `react-router` has moderate advisories that need a move to version 7. One concerns open redirects from unusual link values; the app only passes its own paths. The other concerns server-side rendering, which is not used. Neither was tested for exploitability.
- `npm audit`, development only: `vitest`, `vite` and related packages have advisories, including ones rated critical. They do not ship to users. Fixing them needs major upgrades; the fixed `vitest` was two days old when checked, so it was not adopted.
- `pip-audit` found nothing in the installed Python environment, at the time of checking.
- SSRF protection checks addresses at request time and on each redirect; DNS rebinding between check and connection is not prevented.
- The demo accounts' password is public on purpose. `demo load` refuses to run outside demo mode, so they should never exist on a real deployment. Nothing removes them if demo mode is later switched off on the same database.
- `tenderfit/config.py` has a default local connection string with a placeholder password, used only if `DATABASE_URL` is unset.

## Supplier onboarding

- A person can create a supplier and add, correct and evidence facts. **Facts are typed by hand: no extraction of facts from uploaded company documents exists,** and the only confirmation of a document's facts is the existing check.
- Regions are entered as ITL codes, which most suppliers will not know. There is no CPV picker.
- No bulk import, no invitations, no way to delete a fact or supplier.

## API compatibility

- Every path moved to `/v1`, with no compatibility layer, and all clients must sign in. See [API_MIGRATION.md](API_MIGRATION.md).
- Response bodies have no declared schemas in the OpenAPI document, so it specifies requests much better than responses.

## Accuracy is unmeasured
- **No real-world benchmark exists.** No real model has been run against real tenders in this iteration; no API key was available. All verdict tests use scripted responses.
- The demo's model answers are scripted. They show the machinery, not model quality.
- The default retrieval score threshold is unset, because no evaluated value exists. Reference gaps do trigger a second search.
- Graph retrieval and corrective retrieval are on by default because they only add passages the requirement itself names. Their benefit is unproven.

## Extraction and rules
- The rule engine covers a fixed set of condition types. Everything else goes to the model.
- Fact scope matching uses simple word overlap and a small alias table. "Tables" against "desks" is handled; subtle product categories are not.
- Numbers in words are handled for common forms only. Validation can wrongly drop a good condition, which sends it to the model at higher cost, not to a wrong verdict.
- The contradiction check needs near-identical wording and a shared unit. It will miss differently worded conflicts, and has no evaluated false-positive rate.
- A rule's MET is lowered if the quote cites something unreadable, but a rule does not read the text beyond the quote.
- A model verdict is checked for citations and current facts, not for whether its reasoning follows. Citation entailment is not measured.

## Documents
- **No OCR.** Pages that are images are reported per page and the document is `unreadable` or `partial`, never read. Text on those pages is not used, so no blocker can be claimed from them.
- Image detection uses a heuristic: little text (under 40 characters) and images covering at least 20% of the page. A page that is a vector drawing of text, or that has a small scan, can be misjudged. Tested on generated fixtures, not on a corpus of real PDFs.
- Documents read before migration 018 are not re-judged until processed again.
- Repeated headers and footers are not removed, so a changing footer can make an amendment look larger than it is. That leads to a full reassessment, which is safe but costs more.
- XLSX, ZIP and DOC are not parsed. Large tables stay in one passage, and embeddings truncate at the model's 256-token limit, so the later part of a long passage is not found by vector search. Lexical search still covers it.
- A passage records its first page only, so citations can be a page off.
- Retrieval does not filter by lot; a passage about another lot can be consulted.
- Find a Tender attachments returned 403 to automated clients when last probed, so real packs mostly need uploading.

## Evidence verification
- Checks read the text layer. They cannot detect a forged or altered document.
- Supplier identity is checked by name matching only. Expiry parsing covers common wordings; anything else is reported as not found.

## Amendments
- A lot removed by an amendment stays visible and can still be assessed; it triggers a full reassessment but no specific notice.
- Lot-level cancellation is not modelled: a cancellation notice cancels every lot.
- Status freshness is one global import watermark, not per tender.
- If a re-check moves a supplier's only current assessment onto the new version before the amendment is compared, that supplier gets the current result with no before-and-after. If no other supplier is behind and the event had not yet been created, **no event appears on Changes at all**, though the lot page shows the new version. The window is the time between an import and its comparison.
- Passage-level alignment means one edit reopens every requirement citing that passage.

## Static analysis

- `mypy` reports 0 errors in default mode, and runs in CI. **It is not `--strict`,** `ignore_missing_imports` is on, and JSON columns and rows are typed loosely as `dict[str, Any]`, so it catches less than the number suggests.
- `ruff` runs on a deliberately small rule set. There is no frontend linter.
- API responses are mostly `any` in the frontend, so contract errors surface at runtime.

## Reliability and operations

- **The GitHub Actions workflow has never run on GitHub.** Its commands ran locally, in pieces; see the validation report.
- Migrations are forward-only and hold their locks for the whole run. See [MIGRATION_VALIDATION.md](MIGRATION_VALIDATION.md).
- No backups, metrics or alerting are set up. The worker's only monitoring is its logs.
- Frontend tests cover sign-in, errors, lot navigation and verdict display, with a stubbed server. The browser walkthrough was a one-off script, not in the repository or CI. Tested in a Chromium browser at desktop and 375px widths; not in Safari or Firefox, and not with a screen reader.
- SSRF, `replay` and the destructive path are covered in `tests/`; the destructive path has only run on throwaway databases.

# Known limitations

## Do not use with real supplier data yet
- **No authentication.** Any caller can pass any `profile_id`, and profile scoping is enforced by the API on that value. The isolation work is real and tested, but it protects against mistakes, not an attacker. Authentication was left out on purpose, pending a decision on single-supplier or multi-tenant use.
- `user` on a confirmation is still free text, so the audit trail does not identify a person.
- **No screen or API creates a profile or a fact.** Profiles come from seed scripts. You can edit and add evidence to existing facts only.
- Documents and evidence are unencrypted on a local volume.

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
- No OCR: scanned PDFs are reported unreadable. A scanned PDF with a text cover page is **still counted as read**.
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
- Lot-level cancellation is not modelled: a cancellation notice cancels every lot. The older Desktop copy had finer logic and refresh back-off, which were deliberately not imported unreviewed.
- Status freshness is one global import watermark, not per tender.
- Passage-level alignment means one edit reopens every requirement citing that passage.

## Engineering
- `mypy` reports many errors in the existing code and is not part of CI. `ruff` runs on a deliberately small rule set.
- No frontend unit tests. The UI was checked by hand in a browser at desktop and 375px widths, not with an automated accessibility tool or a screen reader.
- API responses are mostly untyped in the frontend (`any`), so contract errors surface at runtime.
- SSRF protection checks addresses at request time and on each redirect, but a DNS rebinding attack between check and connection is not prevented.
- The read-only static demo build (`scripts/build_demo.py`) was updated but not re-run.
- `replay` in `tenderfit/ingest` truncates tables with CASCADE and destroys assessments. Do not run it on data you want to keep. This was not changed in this iteration.

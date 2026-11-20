# Phase 5A and 5D: scenario evidence provenance

Implemented on `codex/remaining-phases`, starting from `main` at `4e7de56` (PR #10). This milestone preserves and presents the evidence behind deterministic simulated verdicts. Phases 5B/5C and 6–9 remain separate work.

## Behaviour

- Rule decisions retain their original tender citations in both control and simulated records.
- New `sim-v2` snapshots freeze each fact's evidence references, quotes, page, quote-check flag, latest verification level for that fact/file, and human-verification flag. They add a schema marker, including for a supplier with no facts. No migration is needed.
- Old `sim-v1` JSON round-trips without changing its hash. An old draft can run, and its variations retain the old snapshot and engine version. Missing historical support is reported explicitly; it is never reconstructed from today's evidence.
- Every used fact is actual, modified, hypothetical or removed (control only). Change history records ordinal and before/after values. Added facts retain negative IDs and never acquire real evidence references. Modified facts can show the original documents, with an explicit warning that they do not prove the assumed replacement.
- Context parameters and requirement changes are also labelled. A changed threshold never appears to be part of the original tender quote.
- `GET /v1/simulations/{id}/results` adds optional `provenance` and `control_provenance`. `GET /v1/simulations/{id}/results/{requirement_id}/provenance` resolves passage text and evidence metadata. Simulation ownership is checked first; passage documents use `_readable_document`; evidence metadata is scoped to the session's supplier. Missing or inaccessible sources resolve as unavailable. No local storage paths are returned.
- The simulation result table has an inline **Evidence and assumptions** disclosure. It shows both rule runs, quotes, facts, assumptions, changes and uncertainty. Source-resolution failures preserve the frozen record and offer retry. Old completed results still render with an explicit provenance-unavailable message.
- Assessment fact details and simulation fact details share evidence badges. **Verified evidence** needs a matching quote and a `supports`/`current` verification for that fact/file, or human verification. A quote match alone is insufficient. Assumption badges take precedence over historical verification.

## Boundaries

Evaluation remains rules-only, inside the existing read-only transaction. Provenance attaches after the selective/full equivalence check, so descriptive differences cannot conceal a change in the decision. There is no model call, apply route, live-data write, new service or migration.

The snapshot freezes evidence references and verification, not the file bytes. The resolver reports currently accessible metadata; source files may later become unavailable. This is traceability, not a guarantee against forgery. Model-only rows have no simulated rule conclusion. Answers and overrides still follow the existing live-input semantics; freezing and varying them is Phase 5B.

Phase 5D here covers assessment fact details and the individual simulation result. Comparison-page expansion and the broader Phase 8 design work remain outstanding.

## Validation and next work

Regression coverage includes legacy hashes and variations, all nine change kinds, ordered fact histories, frozen support after live edits, citations, selective/full equivalence, ownership, source visibility and live-table fingerprints. Frontend tests cover assumption precedence, verification strength, source resolution, failure and old results.

The Phase 9 intake is at `eval/validation/`: 24 existing procurement pairs listed as calibration candidates, with all human annotation still pending. These exposed pilot cases must not be presented as an unseen test set.

Next: 5C bounded multi-change attribution and 5B frozen answer variation, then Phase 6 gap/fragility analysis. Answer variation must use a new snapshot version (`sim-v3`), preserving the live-answer semantics of both `sim-v1` and `sim-v2`. Phase 7 remains gated by deterministic tools and evaluated human labels.


Validation on 20 November 2026: full backend suite **571 passed, 0 skipped** with `VERSIONRAG_ENABLED=false DEMO_SCRIPTED_LLM=false REGISTRATION_ENABLED=false` (the local demo `.env` otherwise changes test assumptions). The final provenance module is checked separately after the added parameter-history regression. Frontend: **56 passed**, TypeScript and production build passed; Python Ruff and mypy passed. Full Git history and an export of reviewable files passed Gitleaks. The broad working-tree scan flags the pre-existing ignored `.env`, which is excluded from the commit. No paid model run, browser visual review or moderated user study was performed.

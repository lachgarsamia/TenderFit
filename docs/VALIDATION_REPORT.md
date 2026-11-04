# Validation report

**Status: awaiting human review. Nothing has been pushed, merged or deployed.**

| | |
|---|---|
| Branch | `release-hardening` (local only) |
| Based on | `origin/main` at `2b0bce3` |
| Code tested | The tip of this branch; `git log -1` gives the commit. Documents were the only later change |
| Date | 4 November 2026 |

## Summary

**Release recommendation: ready for your code review. Not ready for production, and not ready for real supplier data without the review and the decisions below.** Authentication, authorization and isolation are implemented and tested locally, but have had no independent review. No real model has been run on real tenders here, because no API key was available, so **accuracy is unmeasured.**

Changes: accounts and session-derived supplier identity; non-destructive replay; page-level PDF status; supplier onboarding; a typed data layer; a hardened CI workflow; and API versioning under `/v1`. See [CHANGELOG.md](../CHANGELOG.md).

## Local and remote

| Check | Local | On GitHub |
|---|---|---|
| Backend tests, ruff, mypy | Verified | **Pending** |
| Migrations on an empty database, twice | Verified | **Pending** |
| Frontend type check, tests, build | Verified | **Pending** |
| Secret scan of history and tree | Verified | **Pending** |
| Compose config | Verified | **Pending** |
| Workflow file | Passes `actionlint`, **never executed** | **Pending** |
| `pip install -e '.[dev]'` from nothing, with Linux runners | **Not replicated.** Docker images install runtime dependencies from nothing; `mypy` was added to the dev list and comes from the existing environment locally | **Pending** |

## Test results

| Suite | Tests | Passed | Failed | Skipped |
|---|---|---|---|---|
| Backend (`scripts/test.sh`, `REQUIRE_DB=1`) | 349 | 349 | 0 | 0 |
| Frontend (Vitest, stubbed server) | 14 | 14 | 0 | 0 |

The skip count comes from the JUnit report, not just the console. **All verdict tests use scripted model answers.** Frontend tests do not talk to a real server.

**Browser walkthrough, one-off script** (not in the repository or CI), on the Compose demo in a headless Chromium browser: 21 of 21 checks and then 5 of 5 on evidence flows. Those checks found one real defect, listed below. The only console errors logged were the failed-request lines for the `401`, `403` and `404` responses the walkthrough provokes on purpose. Screenshots are real captures from these runs, in [screenshots/](screenshots/).

### Required coverage

| Requirement | Where |
|---|---|
| A reads B's profile, evidence | `test_isolation.py::test_every_fact_and_evidence_route_refuses_another_supplier`, `test_a_file_of_one_supplier_cannot_prove_anothers_fact` |
| A triggers B's assessment | `test_a_client_supplied_profile_id_is_ignored`, `test_assessment_history_jobs_and_trace_are_scoped` |
| LLM context leakage | `test_a_suppliers_answer_never_reaches_another_suppliers_prompt`, `test_private_text_stays_out_of_retrieval_and_prompts_for_others` |
| Cross-profile amendments | `test_amendment_events_and_retries_are_scoped`, `test_cross_profile_amendment_processing_keeps_each_suppliers_decisions` |
| Unauthorized jobs and assessments | `test_auth.py`, `test_assessment_history_jobs_and_trace_are_scoped` |
| Replay refuses, preserves | `test_replay_safety.py` |
| Fresh and upgrade migration | `test_migrations.py`, [MIGRATION_VALIDATION.md](MIGRATION_VALIDATION.md) |
| Page-level and mixed PDFs | `test_pdf_pages.py` |
| Expired evidence renewal | `test_soundness.py::test_expired_fact_can_be_renewed_and_triggers_reassessment` |
| Lifecycle, failed retry | `test_assessment_lifecycle.py`, `test_amendment_retry.py` |
| API compatibility, validation | `test_api_contract.py`, `test_demo_and_api.py` |
| Rule-engine cases | `test_rules.py`, `test_rules_regression.py`, `test_soundness.py` |
| Frontend navigation and errors | `web/src/test/app.test.tsx` |

## Security audit

**Fixed and tested:** the supplier comes from the session on every route; `profile_id` and `user` cannot be supplied by a client (`test_no_route_accepts_a_supplier_or_user_from_the_client`); another supplier's resources return 404; `401` without a session; `403` without the `X-TenderFit` header; passwords use Argon2id and session tokens are stored only as hashes; sign-in lockout; open sign-up off by default; a job acts only on its named supplier's facts; destructive replay is unreachable from the API and worker.

**Open:** see [LIMITATIONS.md](LIMITATIONS.md). The important ones: no independent review, no password reset, no account disabling command, rate limiting on sign-in only, no TLS in Compose, and uploads made before migration 014 remain public.

**Dependencies:** `pip-audit` on the installed Python environment found no known vulnerabilities. `npm audit` after upgrades: production `react-router` moderate advisories needing a major upgrade; development-only advisories in `vitest` and `vite`, some rated critical. See LIMITATIONS.

## Secret scan

Gitleaks 8.18.4 (`scripts/secret_scan.sh`), findings redacted.

| Target | Raw findings | Outside dependencies and caches |
|---|---|---|
| Whole Git history | 0 | **0** |
| Working tree | 20 | **0** |

The 20 are in `node_modules`, `.venv` and caches, all git-ignored. Manual review: `.env` is ignored and holds placeholders with an empty API key. `.env.example` holds placeholders. `.gitignore` and `.dockerignore` cover `.env`, data, build output and caches. No secret needed rotating. Known deliberate values, neither a secret: the local default connection string in `tenderfit/config.py`, and the published demo password. CI's database password is a throwaway service container's.

## CI

The workflow runs ruff, mypy, migrations on an empty database (twice), the full suite with a required database and a failure on any skip, the frontend type check, tests and build, a full-history secret scan, and Compose validation. There is no frontend linter. **It has never run on GitHub.** No secrets are referenced or needed.

## Database migrations

18 migrations. Fresh, repeated, upgrade from `2b0bce3` with data preserved, failure rollback, backup and restore: all pass. Forward-only. Full report: [MIGRATION_VALIDATION.md](MIGRATION_VALIDATION.md).

## API compatibility

**Breaking for every client except `/health`.** Full table, migration path and versioning: [API_MIGRATION.md](API_MIGRATION.md). The frontend, tests and snapshot script have moved. Contract: [openapi.json](openapi.json).

## PDF processing

Each page is `text`, `needs_ocr` or `blank`. Tested with generated fixtures: all text, all scanned, mixed, blank pages, corrupted files, page references kept, originals preserved. A mixed document is `partial`, names its pages in the interface, and no blocker is claimed from it. **No OCR is performed.** Not tested on a corpus of real tender PDFs.

## Type checking

`mypy` default mode: **0 errors, 0 `type: ignore` comments** across 66 files.

| State | Errors | |
|---|---|---|
| `main` at `2b0bce3`, same settings | 333 | Pre-existing |
| This work, midway | 473 | About 140 new, mostly `[call-overload]` from untyped connection rows in new code |
| Now | 0 | Fixed with a `Conn` type and a `must()` helper for rows |

Remaining debt: not `--strict`, `ignore_missing_imports` on, JSON stays `dict[str, Any]`, frontend responses are mostly `any`.

## Known defects

**Found and fixed in this pass:** an amendment could vanish from Changes, or stick on "Waiting to be processed", if a re-check reached the new version first (`test_a_recheck_that_reached_the_new_version_first_neither_hides_nor_strands_the_amendment`).

**Remaining:**
- If that same race happens before the event is created, and no other supplier is behind, no event is shown. The current result is correct. See LIMITATIONS.
- Facts added by hand trigger a full re-assessment of assessed lots, using model budget within the per-lot caps.
- 40 foreign keys lack an index. See the migration report.
- Rest of LIMITATIONS.md.

## Exact commands

```bash
bash scripts/test.sh
```

```bash
.venv/bin/ruff check tenderfit tests scripts
```

```bash
.venv/bin/mypy
```

```bash
cd web && npm ci && npm run typecheck && npm test && npm run build
```

```bash
bash scripts/secret_scan.sh
```

```bash
docker run --rm -v "$PWD:/repo:ro" -w /repo rhysd/actionlint:1.7.7
```

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml config -q
```

```bash
DATABASE_URL=postgresql://tenderfit:tenderfit@127.0.0.1:55432/postgres .venv/bin/python -m tenderfit.eval regression
```

```bash
.venv/bin/python scripts/snapshot_demo.py && .venv/bin/python scripts/build_demo.py
```

## Earlier iteration, also on this branch

These results are from the previous work and were not all re-examined here, though their tests are in the suite above.

### Implementation matrix

| Milestone | Status | Notes |
|---|---|---|
| **M0** Baseline and CI | Implemented, not fully tested | Baseline in `docs/BASELINE.md`. The CI workflow has never run on GitHub, since nothing was pushed. Its commands were run locally |
| **M1** Crash-safe lifecycle | Implemented and tested | Failure injection at each stage. The banner was seen in its waiting state; a mid-run stage was not watched live |
| **M2** Retryable amendments | Implemented and tested | State machine, attempts, durable marker, per-lot isolation, a lock, retry button |
| **M3** Rule engine | Implemented and tested | All known false verdicts plus property tests. A range is expressed as two conditions; there is no range type |
| **M4** Validated extraction | Implemented and tested | Adversarial fixtures. Quantity, threshold, date and standard live inside `condition`, not as separate fields. Tested on scripted output only |
| **M5.1** Decomposition | Implemented and tested | Real extraction quality unmeasured |
| **M5.2** Graph retrieval | Implemented and tested | Postgres table, one hop, explained in the interface. **Benefit not evaluated** |
| **M5.3** Corrective retrieval | Partially implemented | Second search for unresolved references, and abstaining without a model call. The score threshold is unset by design. No search of structured fields |
| **M5.4** Evidence verification | Partially implemented | Text-layer checks and a separate human confirmation. Cannot detect forgery. Not model-assisted |
| **M5.5** Neuro-symbolic assessment | Implemented and tested | Rules bind the model; deterministic contradiction check |
| **M5.6** Selective prediction | Partially implemented | Reason-based labels and abstention. Not calibrated, and their usefulness is unmeasured |
| **M5.7** Constrained agent | Partially implemented | Added an evidence tool, supplier scoping, conflict reporting and rule binding. **Never run against a real model here**; scripted tests only |
| **M6** Dependencies | Implemented and tested | Compared with full reassessment on synthetic amendments only |
| **M7** Human decisions | Implemented and tested | Overrides, answers, confirmations and renewals |
| **M8** Security and isolation | Partially implemented | Isolation, SSRF, upload limits, non-root containers, restart policies and structured errors done. **No authentication**, pending your decision |
| **M9** Frontend | Implemented, not fully tested | Checked by hand in a browser. No automated UI or accessibility tests |
| **M10** Evaluation | Partially implemented | Harness and metadata done. The B0-B5 comparison is prepared but **has not been run** on a real model. Some metrics need labels that do not exist |
| **Demo and docs** | Implemented and tested | Run from empty volumes through Compose |

### AI assessment changes

| Area | What actually changed | What it is not |
|---|---|---|
| Decomposition | A requirement can hold several conditions, each with its own quote, check and result. All mandatory parts must be met | Not validated on real extraction |
| Graph | `passage_edge` records REFERENCES and DEFINED_BY, built from the text | Not a knowledge graph of suppliers or evidence. One hop only |
| Retrieval | Lexical, vector and named-reference candidates fused and reranked. Named references survive reranking. Each passage records why it was retrieved | No evaluated improvement |
| Evidence | The file's text is checked for supplier, standard or policy type, amount, expiry and quote | Does not use a model; cannot detect forgery |
| Rules | Compatible-fact selection, conflict handling, unit conversion, structured outcomes | Scope matching is simple word overlap |
| LLM | Only open conditions go to the model. It cannot lift a rule's answer. A rejected citation now lowers MET | Reasoning is not checked for entailment |
| Selective prediction | Evidence quality is shown as a reason. No percentages | Not calibrated |
| Agent | Evidence tool, supplier scoping, conflict flag | Value unmeasured |
| Dependencies | Per verdict; rule parameters and the implicit deadline recorded; read edges survive carry-over | Validated on synthetic edits only |

#### The one experiment that ran (synthetic; scripted model)

`python -m tenderfit.eval regression`: 8 controlled amendments, 176 verdicts compared with full reassessment.

| | Static dependencies | Recorded (dynamic) dependencies |
|---|---|---|
| Disagreements with full reassessment | 1 | 0 |
| Missed-change rate | 0.125 | 0.000 |
| Share of verdicts reassessed | 0.318 | 0.398 |
| Reassessment precision | 0.125 | 0.114 |

The one miss is the deadline moving past an insurance expiry. **Scripted answers make this deterministic, and eight edits are not a sample of anything.** It shows the bookkeeping works and is a template for the real experiment. Both approaches reassess mostly verdicts that do not change, so targeting is conservative rather than efficient.


# Human validation package

**Status: awaiting your review. Nothing has been committed, pushed, merged, deployed or published.** All changes are uncommitted in the working tree on `main`, which is the branch you chose.

## A. Executive summary

**The failures the audit found are fixed and covered by tests.** Failed runs no longer lose results or amendments, the known false verdicts are gone, invented numbers cannot become rule inputs, and suppliers cannot influence each other's assessments.

**New capabilities work as mechanisms:** decomposed conditions, reference-following retrieval, contradiction detection, evidence checks, per-verdict dependencies, a redesigned interface and a reproducible demo.

**The most important limit:** no real model has been run on real tenders in this work, because no API key was available. All verdict tests and the demo use scripted model answers. **This work shows the system behaves correctly around the model. It does not show how accurate it is.** Please present it that way.

Two gaps remain before any real supplier data: there is no authentication, and no way to create a profile or fact.

## B. Implementation matrix

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

## C. Architecture changes

Diagrams are in [ARCHITECTURE.md](ARCHITECTURE.md). In short:

- **Assessment runs** hold the old result current until the new one commits, in one transaction.
- **Amendments** are a state machine with a durable preparation marker and a processing lock.
- **New modules:** `extraction/validate.py`, `retrieval/graph.py`, `assessment/contradiction.py`, `profile/verify.py`, `demo/`, `eval/regression.py`.
- **New frontend:** tender page, source dialog, progress banner, error boundary, evidence form.
- **New services:** none. No new database, and no multi-agent design.

Six migrations, 011 to 016. Fresh install and upgrade from 010 are both tested.

**API changes you should know about:** `profile_id` is now required on confirmations, answers, supplier uploads, fact edits and the trace endpoint. `GET /lots/{id}/assessment` adds `processing`, `last_failure`, `rejections` and `lots`, and can return `assessment_id: null` with a failure. The frontend and tests are updated.

## D. AI assessment improvements

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

### The one experiment that ran (synthetic; scripted model)

`python -m tenderfit.eval regression`: 8 controlled amendments, 176 verdicts compared with full reassessment.

| | Static dependencies | Recorded (dynamic) dependencies |
|---|---|---|
| Disagreements with full reassessment | 1 | 0 |
| Missed-change rate | 0.125 | 0.000 |
| Share of verdicts reassessed | 0.318 | 0.398 |
| Reassessment precision | 0.125 | 0.114 |

The one miss is the deadline moving past an insurance expiry. **Scripted answers make this deterministic, and eight edits are not a sample of anything.** It shows the bookkeeping works and is a template for the real experiment. Both approaches reassess mostly verdicts that do not change, so targeting is conservative rather than efficient.

## E. Test report

Run on 2026-11-03 against PostgreSQL 16.15 with pgvector.

| Command | Result |
|---|---|
| `bash scripts/test.sh` | **259 passed, 0 failed, 0 skipped**, 69s |
| `.venv/bin/ruff check tenderfit tests scripts` | Passed, on a small configured rule set (E, F, I, without line length) |
| `.venv/bin/mypy tenderfit --ignore-missing-imports` | **473 errors**, up from 333. Not configured, not enforced, not fixed |
| `cd web && npx tsc -b` | Passed |
| `cd web && npm run build` | Passed |
| `docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build` from empty volumes | All 4 services up, API healthy, 16 migrations |
| `python -m tenderfit.demo load` in the container | Passed, with the real embedding and reranking models |
| `python -m tenderfit.demo amend --fail-first`, then Retry in the UI | Failed visibly, then applied through the real worker |
| `python -m tenderfit.demo amend --audit` | 3 impacts; full re-check agreed on all 3 |
| Without a database, `REQUIRE_DB=1` | Tests error, as intended |
| Without a database, local | Tests skip, as intended |

**Existing tests I changed, so you can judge them:**
- `test_experience_window...` asserted NOT MET; it now asserts NEEDS CONFIRMATION. A contract list omitting something is not contrary evidence.
- Two insurance tests now state the policy type, because an unstated type no longer settles by rule.
- `test_api_assessment` uses a far-future deadline to fix the date bug, and now passes `profile_id`.
- `test_eval_run` moved to the new arm names.
- `facts_in` in `test_pipeline` now handles the answers section in prompts.

**Not run:** any real model, the live Find a Tender import on purpose, GitHub CI, Playwright or other automated browser tests, an accessibility tool, a screen reader, the static demo build, load tests.

**Manual browser checks:** Opportunities, Tender, Lot, Changes (failed, waiting, applied), Profile, source dialog, evidence form. At 375px the lot page has no horizontal page overflow; other pages were not checked at that width. Console clean in the final flow. With the API stopped, the app shows a message rather than a blank page. The Preparation page was checked through its API, not visually.

## F. Security report

**Done and tested:** answers scoped by profile; uploads private to the uploader; `confirmed_none` per supplier; private passages excluded from search, graph, inventory and source viewing; assessment plans read only that supplier's edges; verdict cache keyed by profile; profile and ownership checks on routes; document status cannot be set directly; upload size and type checks; SSRF checks on each redirect hop; the agent has no URL input and is read-only.

**Done, not exercised by tests:** non-root containers (confirmed `uid=1000`), restart policies, local-only ports, required database password, structured 500 responses (tested).

**Remaining:**
- **No authentication or authorisation.** Isolation holds for correct callers. Anyone who can reach the API can pass any `profile_id`.
- `user` is free text.
- Documents are not encrypted at rest.
- DNS rebinding between check and connection is not prevented.
- Uploads made before migration 014 stay public, as their owner is unknown.

**Decision for you:** single-supplier research tool, or multi-tenant? It decides how much authentication is needed.

## G. Frontend report

**Added:** tender page, all lots reachable with a lot switcher, real version labels, every affected lot shown, processing state that survives reload, failure and retry states, application error boundary, source reading with highlighted quote and open-at-page, evidence and renewal form replacing the browser prompt, verification ladder, sector-neutral search defaulting to the supplier's own products.

**Requirement panel now shows:** what the tender requires, required against what you have, separately checked conditions, your facts with file and page, why this result, how it was decided (fixed rule, AI reading checked, lowered by a safeguard), what remains uncertain, the next step, and documents consulted with the reason.

**Removed:** the misleading "sent for a closer look" text, and silent error catches on the affected pages.

Demonstrate it with [DEMO.md](DEMO.md).

## H. Professor demonstration guide

[DEMO.md](DEMO.md) has the 15-minute script, expected results, talking points and likely questions. **Rehearse it once: the first load downloads models.**

## I. Research opportunities

[RESEARCH_ROADMAP.md](RESEARCH_ROADMAP.md) has six questions and how to test them. The strongest is whether LLM decisions can be updated incrementally with a measured miss rate. All of them need a labelled real dataset, which is the next real step.

## J. Remaining issues

Full list in [LIMITATIONS.md](LIMITATIONS.md). The ones that matter most:

1. Accuracy on real tenders is unknown.
2. No authentication, and no way to create profiles or facts.
3. A scanned PDF with a text cover page still counts as read.
4. Page furniture can make amendments look larger than they are, which is safe but costly.
5. `replay` still destroys assessments. Not changed.
6. Lot-level cancellation and per-tender refresh exist only in the older copy now in your Trash.

**Things that happened during this work, for your awareness:**
- A test worker made read-only requests to the public Find a Tender API, importing 575 real public notices into a throwaway database, which I then deleted. No account was involved and nothing was submitted. I added `IMPORT_ENABLED` as a result.
- I started Docker Desktop, downloaded Python and Node packages, Docker images and the two Hugging Face models.
- A real race between the worker and a command appeared in the final run and was fixed with a lock.

## K. Human validation checklist

1. **Run the demo** per [DEMO.md](DEMO.md). The stack is running now at http://localhost:8080.
2. **Read the rule policy** in `tenderfit/rules/engine.py`. Do you agree a thin contract list gives NEEDS CONFIRMATION rather than NOT MET?
3. **Check the migrations**, `migrations/011` to `016`, especially the repair in 011.
4. **Run the tests:** `bash scripts/test.sh`.
5. **Decide on authentication** (section F).
6. **Check the API changes** in section C against anything else that calls this API.
7. **Decide whether to commit,** as one change or split by milestone. I did not commit, as you did not ask me to.
8. **Decide about the old copy in your Trash:** the design report, `notes/DECISIONS.md` and `CLAUDE.md` are only there. Keep them before emptying it.
9. **Try a real model** if you have a key: remove the demo overlay, set `ANTHROPIC_API_KEY`, and assess one real tender.

## L. Git status

| | |
|---|---|
| Branch | `main` |
| Starting commit | `2b0bce3cc29e45a049c240efa379d4124b54f05e` |
| New commits | **None** |
| Tracked files modified | 57 (+2,640 / −968 lines) |
| New files | 32 (including `.github/`, `docs/`, `tenderfit/demo/`) |
| Files deleted | None |
| New migrations | `011_assessment_lifecycle`, `012_change_processing`, `013_requirement_conditions`, `014_supplier_isolation`, `015_retrieval_graph`, `016_evidence_verification` |
| Tests | 259 passed, 0 failed, 0 skipped |
| `origin/main` | Still `2b0bce3` |

**Confirmed: nothing was pushed, merged, deployed, published or released.**

`git status` gives the exact current list, which now also includes these reports. `.env` (placeholder values) and `.claude/launch.json` exist locally and are ignored by git.

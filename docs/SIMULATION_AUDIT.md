# Stage 1 audit: what exists, and what a simulation engine can reuse

Audited on branch `feature/isolated-simulations` at `6669600` (`origin/main`, 2026-11-15). This audit is based on the source code and
tests. README and docs files were used only as hints. No application code was changed.

`TenderFit_Technical_Audit.md` (untracked, repository root) describes `2b0bce3`, which is 12 commits older and predates the
VersionRAG work (`tenderfit/retrieval/versioned.py`, `PipelineConfig.versionrag`). Where the two documents disagree, this one wins.

---

## 1. The central finding

The verdict logic is already split into a **pure core** and a **write-heavy shell**. The shell's writes go to tables that
live and shadow runs share, so they are only told apart by `WHERE` filters.

```
             PURE (in-memory inputs → result)                       WRITES (shared live tables, commits mid-run)
 ┌──────────────────────────────────────────────┐      ┌─────────────────────────────────────────────────────────┐
 │ rules.engine.evaluate(Condition, [Fact], Ctx)│      │ assess_lot → _begin  (INSERT assessment, COMMIT)        │
 │ pipeline.parent_status / _from_rules /       │      │   _run → process_version, index_version (COMMIT),       │
 │          _combine / guard / evidence_quality │      │          build_graph (COMMIT), extract_version          │
 │ profile.validity.check                       │      │        → _progress (UPDATE assessment, COMMIT) ×N       │
 │ rules.aggregate.dimensions                   │      │        → INSERT verdict + _record_verdict_links         │
 │ changes.impact.plan_reassessment             │      │          (citation, fact, condition, dependency, question)│
 └──────────────────────────────────────────────┘      │        → supersede previous + finish (COMMIT)           │
           ▲ called by                                 └─────────────────────────────────────────────────────────┘
 ┌──────────────────────────────────────────────┐
 │ pipeline.decide(conn, version, req, facts,   │  reads DB; only two writes: llm_call, verdict_cache (no commit)
 │                 ctx, llm, budget, …)         │  (+ agent_step if the agent runs, which needs an assessment id)
 └──────────────────────────────────────────────┘
```

`decide()` (`tenderfit/assessment/pipeline.py:327`) takes the supplier's facts (`list[Fact]`) and the tender context
(`Context`) as **arguments**. They are not read inside it. That's the seam for simulation: a simulation can build modified
facts or context in memory and ask the existing logic what it would decide, without changing any live row.

---

## 2. Assessment

| | |
|---|---|
| **Entry point** | `assess_lot()` `pipeline.py:538` → `_run()` `pipeline.py:599` |
| **Per-requirement decision** | `decide()` `pipeline.py:327` |
| **Rule path** | `_rule_outcomes()` `:176` reads `requirement_condition` rows → `rules.engine.evaluate()` `engine.py:396` → `_from_rules()` `:211` |
| **Model path** | `_model_decision()` `:260`: retrieval, then `verdict_cache` lookup, then LLM, then `guard()` `:105` (deterministic downgrades) |
| **Combining** | `_combine()` `:244`: a rule result always binds the model ("rules bind the model") |
| **Parent status** | `parent_status()` `:198`: any `not_met` wins, all `met` means met, otherwise `needs_confirmation` or `unknown` |
| **Evidence quality** | `evidence_quality()` `:402` → strong / weak / contradictory / missing / ambiguous |
| **Lot-level outcome** | `rules.aggregate.dimensions()` `aggregate.py:66` → recommendation |
| **Persistence** | `_run` `:661` INSERT `verdict`; `_record_verdict_links()` `:566` writes `verdict_citation`, `verdict_fact`, `verdict_condition`, `dependency`, `question` |
| **Lifecycle** | `assessment.status` ∈ running/done/failed (`006`, `011`); `superseded` flag; `_begin` `:494`, `_mark_failed` `:517`, `recover_stale_assessments` `:526` |
| **Current vs history** | Partial unique index `assessment_one_current` (`011`): one `done AND NOT superseded AND NOT shadow` row per (lot, profile). Old rows are kept with `superseded = true`. |
| **Carry-over** | `_carry_verdict()` `:438` copies an old verdict and its links into a new assessment (targeted reassessment) |
| **Configuration** | `PipelineConfig` + `BASELINES` (`assessment/config.py`): B0–B5, VersionRAG; frozen dataclass |

**How a verdict is decided:** first, deterministic rules evaluate each machine-checkable condition. If every mandatory condition
is settled by rule, no model is called. Otherwise the model interprets the conditions that are still open, and its answer is
then guarded: unverifiable quotes are rejected, and stale or declared-only facts are downgraded. Finally the rule results are
re-imposed. Agent escalation (`_run` `:643`) runs only when B2 escalates, and its result is guarded and combined in the same way.

**Reusable for simulation:** `evaluate`, `parent_status`, `_from_rules`, `_combine`, `guard`, `evidence_quality`,
`aggregate.dimensions`, `load_facts` (as a reader), `lot_context` (as a reader), `Decision`, `Fact`, `Context`, `Condition`.

**Not reusable as is:** `assess_lot`, `_run`, `_begin`, `_record_verdict_links`, `_carry_verdict`. All of them write to live tables.

---

## 3. Side effects, function by function

This is the table the isolation design must respect.

| Function | Reads | **Writes** | Commits? |
|---|---|---|---|
| `rules.engine.evaluate` | nothing | nothing | – |
| `pipeline.guard`, `_from_rules`, `_combine`, `parent_status`, `evidence_quality` | nothing | nothing | – |
| `pipeline.load_facts` `:61` | `profile_fact`, `lot_fact_confirmation` | – | – |
| `pipeline.lot_context` `:73` | `lot`, `opportunity`, `notice_version` | – | – |
| `pipeline._rule_outcomes` `:176` | `requirement_condition` (**by requirement id**) | – | – |
| `pipeline._model_decision` `:260` | `question`, `passage`, `passage_edge`, `verdict_cache` | **`llm_call`** (`log_call` `:313`), **`verdict_cache`** (`:314`) | no |
| `retrieval.search.search`, `graph.expand`, `graph.reference_gaps`, `versioned.resolve`, `versioned.provenance`, `contradiction.find_conflicts` | passages, edges, refs | – | – |
| `retrieval.search.index_version` `search.py:64` | passages | `passage.embedding` | **yes** |
| `retrieval.graph.build_graph` `graph.py:20` | passages | `passage_edge` | **yes** |
| `documents.service.process_version` | – | `document`, `passage`, files on disk; HTTP | yes |
| `extraction.extract_version` | passages | `requirement`, `requirement_condition`, `dependency`, `requirement_rejection`, `llm_call` | by default |
| `agent.loop.run_agent` `loop.py:213` | many | **`agent_step`** (FK `assessment_id NOT NULL`), `llm_call` | no |
| `pipeline._begin` / `_progress` / `_mark_failed` | – | `assessment` | **yes, every call** |
| `pipeline._run` (end) | – | supersedes the previous `assessment`, finishes this one | yes |

**Consequence:** "run `assess_lot` inside a transaction and roll back" is **not** a safe isolation strategy. It commits at
`_begin`, at every `_progress`, inside `index_version`, and inside `build_graph`.

---

## 4. Why `shadow=True` is not a simulation

`shadow` changes exactly three things in `assess_lot`:

1. `_begin` `:497` does not close the other running assessments.
2. The row is inserted with `shadow = true, superseded = true` (`:506`).
3. `_run` `:689` does not supersede the current assessment.

Everything else is identical. In particular:

| Problem | Where | Why it matters |
|---|---|---|
| **No way to inject a hypothesis** | inputs are read from `profile_fact`, `requirement_condition` and `notice_version` inside `_run` and `decide` | To simulate "insurance threshold £10M" with shadow, you'd have to **edit the live requirement or fact first**. That's the forbidden mutation. |
| **Shadow rows live in the live tables** | `assessment`, `verdict`, `verdict_*`, `dependency`, `question`, `agent_step` | Isolation depends on every query remembering `NOT shadow` / `NOT superseded`. The current code is careful: `api/assessments.py:62`, `profile/service.py:279`, `changes/apply.py:313`. But it's a convention, not a constraint. |
| **Shadow verdicts are reachable by id** | `assessment/confirm.py:28` `_verdict()` does not check `shadow` | A G2/G3/G6 confirmation posted with a shadow verdict's id is accepted for the same profile. G6 `override` then `UPDATE`s that verdict. |
| **Questions leak by requirement** | `_record_verdict_links` `:594` writes `question(requirement_id, profile_id)`; `_model_decision` `:264` reads answers by **requirement lineage + profile**, not by assessment | An answer to a shadow run's question would feed later live runs. |
| **Shared pre-processing** | `_run` `:603–608` runs `process_version`, `index_version`, `build_graph`, `extract_version` | A shadow run can fetch documents, extract requirements and commit them to shared tender data. |
| **No baseline reference** | `assessment` has no `baseline_assessment_id` | Only `change_impact.shadow_assessment_id` ties a shadow run to anything. |
| **Uses live, current facts** | `load_facts` reads `profile_fact` at run time | Not what the baseline saw, if facts changed since (see §7). |

**Verdict:** shadow is a correct tool for its job, which is auditing targeted reassessment against a full run (`shadow_full`
`changes/apply.py:283`). It is unsafe and the wrong shape for hypothetical experiments.

---

## 5. Hazard found: the verdict cache could be poisoned by a simulation

`_cache_key()` `pipeline.py:161` hashes the prompt version, profile, version, **requirement id**, dates, passage ids,
answers, config, open-condition labels and fact values.

It does **not** include the requirement's statement, quote, or condition parameters. In live operation that's correct,
because a requirement row never changes. But a simulation that changed a requirement's wording and reused
`_model_decision` would:

1. **read** the live verdict for that requirement id from the cache, so the hypothesis is silently ignored, and
2. if there was no cached entry, **write** the hypothetical verdict under the *live* key. The next real assessment
   would then use the hypothetical result.

A simulation must never write to `verdict_cache`. If it ever uses the model path, it needs a key that includes the
modified requirement. This is one of the side effects the refactor in Stage 5 has to remove.

---

## 6. Retrieval

| Piece | Where | Notes |
|---|---|---|
| Hybrid search | `search()` `search.py:96` | Postgres `tsvector` lexical results and pgvector cosine results, fused by RRF (k=60), then cross-encoder rerank; explicit graph links always survive. Every hit records `why`. Supplier access is filtered by `owner_profile_id`. |
| Embedder / reranker | `MiniLM`, `CrossEncoderReranker` `search.py:30,40`; `Embedder` / `Reranker` Protocols | Tests inject `BagEmbedder` / `OverlapReranker` (`tests/test_retrieval.py`). |
| Corrective retry | `_model_decision` `:279`, `corrective_query()` `search.py:146` | Bounded by `settings.corrective_max_attempts` |
| Passage graph | `passage_edge` (`015`), `build_graph()` `graph.py:20`, `expand()` `:53` | REFERENCES / DEFINED_BY edges come only from text, so every edge can be explained. Postgres only. |
| Version-aware paths | `versioned.resolve()` `versioned.py:15` (2-hop), `provenance()` `:63`, `reusable()` `:88` | Pinned to one `notice_version`; `provenance` raises if evidence comes from outside the snapshot. |
| Provenance on verdicts | `verdict.retrieval` jsonb, `verdict_citation`, `verdict.reference_gaps` | Citations are verified by `quote_ok` in `guard`. |

**Reusable:** all read-side functions, as they are. **Missing:** nothing needed for the first simulation milestone.
Rule-only simulations don't call retrieval at all.

---

## 7. Versioning: tender is versioned, supplier profile is not

| | Tender | Supplier profile |
|---|---|---|
| Version table | `notice_version` with an **immutability trigger** (`001`, `forbid_update`) | **none** |
| Requirements | `requirement.version_id`; carried across versions via `carried_from` | – |
| Facts | – | `profile_fact` is **updated in place** (`profile/service.py:163 update_fact`, `daily_check :282`) |
| What a verdict records | `assessment.version_id` (exact) | `verdict_fact(verdict_id, fact_id)`: the fact's **id**, not its value at the time |
| Change detection | `change_event` + `diff.py` | `profile_fact.updated_at` only |

**Consequence for the brief's "supplier profile version used":** that reference can't be made to an existing record,
because none exists. The options are, for Stage 3 to decide:

- **(a) Snapshot** the `Fact` list the simulation used, inside the simulation record (small: a profile has tens of facts).
- **(b) Add profile versioning** to the live system. That's larger, and it touches Mode A.

Either way, `profile_fact.updated_at > baseline.finished_at` detects that a baseline was computed on facts that have since
changed. A simulation should record that, so a comparison never silently pits "hypothesis" against "drift".

---

## 8. Dependency tracking and impact analysis

| Piece | Where | Notes |
|---|---|---|
| Storage | `dependency` (`002`, `006`): `requirement_id, version_id, kind, target_type, target_id, recorded_by, verdict_id` | kinds: source / reference / definition / parameter / read / fact; targets: passage / field / document / fact |
| Static deps (extractor) | `extraction/extract.py:74 _record_dependencies` | `verdict_id IS NULL` |
| Dynamic deps (per verdict) | `_record_verdict_links` `pipeline.py:566` | facts used, parameters read (`notice.deadline` added whenever a dated fact is used), passages read, documents opened |
| Loading | `load_deps(conn, assessment_id)` `impact.py:45` | Scoped to one assessment, so other suppliers and shadow runs have no say |
| Planning | `changed_targets()` `:25`, `plan_reassessment()` `:33` | **Pure.** Splits affected requirements into `rules_only` (only parameter deps hit, so no model call) and `reassess`. Structural or ambiguous change means full reassessment. |
| Fact-level reverse lookup | `profile.service.verdicts_using()` `:276` | Live, current assessments only |
| Applying | `apply_version_change` `apply.py:216`, `apply_profile_change` `:407` | Both end in `assess_lot` with `carry=` (verdicts to keep) |
| Self-check | `shadow_full` `apply.py:283` | Full shadow run; any mandatory disagreement is recorded as a missed dependency |

**Reusable for simulation:** `load_deps` + `plan_reassessment` answer "which requirements does this hypothetical touch?"
for fact changes (`("fact", id)`) and field changes (`("field", "notice.deadline")`) directly. Postgres is enough here, so a
graph database isn't justified.

**Missing:**
- There's no dependency target for a **requirement's own condition**. A threshold change hits that requirement by definition,
  so the simulation adds it directly rather than through `dependency`.
- `plan_reassessment` takes a `Change` (an amendment diff). A hypothetical needs a small adapter that produces the
  targets (facts / fields / requirements), not a fake `Change`.

---

## 9. Amendment processing

`process_pending` `apply.py:347` (advisory lock) → `detect` `:28` (diff and classify, then `change_event`) →
`apply_version_change` `:216` → `carry_requirements` / `prepare_full` (one transaction, durable `version_preparation`
marker) → per (lot, profile): `plan_reassessment` → `assess_lot(carry=…, finalise=…)`. `finalise` writes `change_impact`
inside the run's final transaction. Retries: `change_event.status/attempts`, back-off, `retry_event`.

This is real, persistent processing of real amendments. Per the brief, **simulation must not route through it.** Its
`explain()` `:193` (before → after text) and the `affected` list format are worth copying for simulation comparisons.

---

## 10. Agent

| | |
|---|---|
| Loop | `run_agent()` `agent/loop.py:213`: one requirement, tool use, at most `settings.agent_max_tool_calls` |
| Tools | `Tools` `:103`: `search_passages`, `read_section`, `open_attachment`, `check_clarifications`, `read_profile`, `check_evidence`, `compare` (the rule engine), and the finish tools `record_verdict` / `raise_question`. All read-only. |
| Budget | `Budget` `:18`: cost / token / time caps, shared across a lot |
| Validation | Tool names are checked against `TOOLS`; bad arguments become error results; the final answer goes through the same `guard()`; prompt-injection rule `UNTRUSTED_RULE`, passages wrapped by `untrusted()` |
| State | `Tools.seen` (passages it may cite), `read_docs`, the message list |
| Persistence | `agent_step` per call (**needs an `assessment_id`**), `llm_call` |
| Integration | Called from `_run` only when B2 escalates and the agent is enabled |

The agent investigates one requirement. It doesn't plan investigations. `Tools` takes `facts` and `ctx` **in memory**, so it
can already be pointed at simulated facts. That's useful for Stage 7. Its writes (`agent_step`) are tied to `assessment`,
so the agent must stay out of simulations until it has a simulation-scoped trace table.

---

## 11. API, schema, frontend, tests

**API:** FastAPI, `/v1` router with a CSRF header check (`api/main.py:39`). The profile always comes from the session
(`api/deps.py:41 current_profile`), never from the request. Long work goes through the `job` table and the worker
(`jobs/worker.py`). The assessment read model is `get_assessment()` `api/assessments.py:96`. OpenAPI is exported to `docs/openapi.json`.

**Schema / migrations:** 18 plain SQL files, applied in name order by `db.migrate()` `db.py:25` under an advisory lock,
tracked in `schema_migration`. There are no down-migrations. CI checks that a second run applies nothing, and
`tests/test_migrations.py` exists. Conventions: `bigserial` ids, `CHECK` constraints for enums, partial unique indexes for
lifecycle invariants, triggers for immutability (`forbid_update`). **The next migration is `019_…sql`.**

**Frontend:** React 18 + react-router 6 + Vite, about 1.6k lines. `web/src/api.ts` is a fetch wrapper to `/api/v1` with a
static-snapshot demo mode. Pages: Opportunities, Tender, Assessment (388 lines), Changes, Profile, Preparation. There is no
simulation UI, and none is needed for the first milestone.

**Tests:** pytest against a real throwaway pgvector Postgres (`tests/conftest.py`; `scripts/test.sh` starts
`pgvector/pgvector:pg16` in Docker on port 55432). `REQUIRE_DB=1` turns skips into failures. Every test truncates all tables.
Fakes: `ScriptedLLM` (`tests/fakes.py`), `BagEmbedder` / `OverlapReranker`, and the `seed()` profiles (Atlas Furniture,
Pennine Seating) plus the Northfield tender (`tests/northfield.py`). The `world` fixture in `tests/test_isolation.py:28` is
the natural base for simulation tests. Web: vitest + testing-library. CI (`.github/workflows/ci.yml`) runs ruff, mypy,
the migration idempotency check, pytest with no skips allowed, the web typecheck, tests and build, a secret scan, and a compose config check.

**Test gap:** no existing test asserts that a shadow run leaves the live verdict rows unchanged. `tests/test_versionrag.py:70`
compares statuses only.

---

## 12. What is missing for the target architecture

1. A **side-effect-free evaluation entry point**: the `decide()` logic with conditions passed in instead of read by
   requirement id, and with no `llm_call` / `verdict_cache` writes. A rule-only mode is fully deterministic.
2. **Typed, validated hypothetical changes** (threshold, certificate, fact expiry, fact removal, deadline), with no free-form
   SQL or dict patches.
3. **Simulation persistence that is separate by construction**: its own tables, so no live query can ever see simulation rows.
   That's stronger than a flag.
4. **A profile snapshot** in the simulation record (§7).
5. **A hypothesis-to-targets adapter** for `plan_reassessment` (§8).
6. **Baseline/simulation comparison** (status deltas, recommendation delta, evidence-quality delta).
7. Later: a simulation-scoped agent trace, then an investigation-level agent.

## 13. Risks to carry into Stage 3

- **R1:** Reusing `assess_lot` / `_run` for simulation writes to live tables and commits mid-run. *Don't.*
- **R2:** Verdict-cache poisoning (§5).
- **R3:** The model path isn't deterministic, so it costs money and can drift. Start with rule-settled requirements, and
  report "needs the model" explicitly rather than guessing.
- **R4:** A baseline computed on facts that have since changed (§7) produces a misleading comparison. Detect it and record it.
- **R5:** Shadow verdicts accepted by confirmations (§4). This is a pre-existing Mode A issue, outside this milestone's scope. Report it separately.
- **R6:** Refactoring `decide()` touches Mode A. Any change must be behaviour-preserving and covered by the existing
  pipeline and soundness tests before and after.

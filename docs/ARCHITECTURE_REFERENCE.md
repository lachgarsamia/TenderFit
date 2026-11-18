# TenderFit architecture reference

A technical reference for `main` at `bfe4015` (18 November 2026). It covers the module map, the data flows, the database, the agents, and the invariants that hold the system together.

- New to the project? Read [TEAM_HANDOFF.md](TEAM_HANDOFF.md) first.
- For what comes next, see [PRODUCT_ROADMAP.md](PRODUCT_ROADMAP.md).
- Everything here was checked against the source on `main`. Where an older document disagrees, the code wins, and the conflict is listed in [TEAM_HANDOFF.md §13](TEAM_HANDOFF.md#13-where-older-documents-disagree-with-the-code).

Contents

1. [System overview](#1-system-overview)
2. [Module map](#2-module-map)
3. [Mode A: the assessment lifecycle](#3-mode-a-the-assessment-lifecycle)
4. [Hybrid retrieval](#4-hybrid-retrieval)
5. [Amendments and dependencies](#5-amendments-and-dependencies)
6. [Mode B: simulations](#6-mode-b-simulations)
7. [The what-if agent](#7-the-what-if-agent)
8. [Scenarios, variations and comparison](#8-scenarios-variations-and-comparison)
9. [Jobs and the worker](#9-jobs-and-the-worker)
10. [Database](#10-database)
11. [API surface](#11-api-surface)
12. [Frontend](#12-frontend)
13. [Invariants and where they are enforced](#13-invariants-and-where-they-are-enforced)
14. [Configuration reference](#14-configuration-reference)

---

## 1. System overview

```mermaid
flowchart TB
  subgraph External
    FTS[Find a Tender OCDS API]
    Portal[Public attachment URLs]
    Anthropic[Anthropic Messages API]
  end

  subgraph Web["web/ · React 18 + TypeScript + Vite, served by nginx"]
    ModeAUI[Opportunities · Tender · Assessment · Changes · Profile · Preparation]
    ModeBUI[What-ifs tab · Ask panel · Simulation page · Scenario builder · Compare]
  end

  subgraph API["tenderfit/api · FastAPI, one uvicorn process, sync handlers"]
    Deps[deps.py: session → user → supplier · CSRF header]
    RoutesA[assessments.py · screens.py · accounts.py · main.py]
    RoutesB[simulations.py: the only API module that imports tenderfit.simulation]
  end

  subgraph Worker["tenderfit/jobs · one worker process, Postgres job queue"]
    Q[queue.py: claim · fail · backoff · priority]
    H[worker.py handlers: import · assess · detect_changes · shadow_full · profile_change · daily_check · simulation_agent]
  end

  subgraph ModeA["Mode A domain: what is true now"]
    Ingest[ingest]
    Docs[documents]
    Extract[extraction]
    Retr[retrieval: search · graph · versioned]
    Rules[rules: engine · aggregate]
    Pipe[assessment: pipeline · confirm · contradiction]
    AgentA[agent/loop.py: bounded per-requirement agent]
    Changes[changes: diff · impact · apply]
    Profile[profile: service · verify · validity]
    Auth[auth]
  end

  subgraph ModeB["Mode B domain: what could happen · tenderfit/simulation"]
    Store[store: the only writer of simulation rows]
    Snap[snapshot · inputs · scenario · changes]
    Engine[engine: READ ONLY evaluation]
    Imp[impact · compare · scenarios]
    AgentB[agent.py + spend.py]
  end

  DB[(PostgreSQL 16 + pgvector<br/>live tables · simulation tables)]
  Files[(Document volume /data)]

  ModeAUI -->|/api/v1| Deps
  ModeBUI -->|/api/v1| Deps
  Deps --> RoutesA & RoutesB
  RoutesA --> ModeA
  RoutesB --> ModeB
  RoutesA -->|enqueue| DB
  RoutesB -->|enqueue simulation_agent| DB
  DB -->|claim| Q --> H
  H --> ModeA
  H -->|simulation_agent only| AgentB
  Ingest --> FTS
  Docs -->|public addresses only, SSRF-checked| Portal
  Docs --> Files
  Extract & Pipe & AgentA & AgentB --> Anthropic
  ModeA --> DB
  Engine -->|reuses decide, read only| Pipe
  ModeB --> DB
```

Key points:

- **One database and one queue.** The passage graph is a table (`passage_edge`), and the queue is a table (`job`). There is no Redis, no graph database and no vector service.
- **Mode B reuses Mode A's verdict logic but never writes to Mode A's tables.**
  - `simulation.engine` calls `assessment.pipeline.decide(..., interpret=False)` inside a Postgres `READ ONLY` transaction.
  - Only `api/simulations.py` and `jobs/worker.py` may import `tenderfit.simulation`. The test `tests/test_simulation.py::test_mode_a_code_never_imports_the_simulation_package` enforces this.
- **Models propose and code decides.** Extraction output is validated before it is trusted. In Mode A, a model verdict passes through `pipeline.guard()` and is bound by rule results (`_combine`). In Mode B, simulated verdicts come from rules only, and the model can only propose typed changes.

---

## 2. Module map

Paths are relative to the repository root. "Must not depend on it" names the boundary to protect.

### 2.1 Infrastructure

| Module | Responsibility | Inputs → outputs | Depends on | Must not depend on it |
|---|---|---|---|---|
| `tenderfit/config.py` | `Settings` (pydantic-settings, reads `.env`); one module-level `settings` | environment → typed settings | — | — |
| `tenderfit/db.py` | `connect()` (psycopg 3, `dict_row`), `migrate()` (numbered SQL files under the advisory lock `42`, recorded in `schema_migration`), `must()` | URL → `Conn` | config | — |
| `tenderfit/llm.py` | The `LLM` protocol (`json`, `tool_turn`) and its implementations: `AnthropicLLM`, `RecordedLLM` (replays or records), plus `default_llm()`, `log_call()`, `untrusted()`, `PRICES`, and the `LLMError`/`OutputTooLong` errors with `may_have_billed`/`usage` | prompts → `JsonResult` / `ToolTurn` + `Usage` | anthropic SDK | — |
| `tenderfit/demo/llm.py` | `DemoLLM`: the fictional demo's scripted answers (`DEMO_SCRIPTED_LLM=true`) | — | — | Real-tender code paths must never select it |
| `tenderfit/jobs/queue.py` | `enqueue`, `enqueue_unique`, `claim` (`FOR UPDATE SKIP LOCKED`, lock keys, stale reclaim, low priority last), `complete`, `fail` (exponential backoff), `pending`, `waiting_behind` | job rows | db | — |
| `tenderfit/jobs/worker.py` | The `HANDLERS` table; schedules imports, daily checks and amendment processing; recovers stale assessments and simulations; the main loop | claimed jobs → domain calls | queue, domain modules, `simulation.agent` / `engine` (allow-listed) | — |
| `tenderfit/auth/` | Accounts (Argon2id), sessions (random tokens stored as SHA-256), membership, login lockout; CLI `python -m tenderfit.auth` | credentials → session | db | — |

### 2.2 API (`tenderfit/api/`)

| Module | Responsibility |
|---|---|
| `main.py` | The FastAPI `app`. It runs `migrate()` on startup and installs a catch-all 500 handler that logs internals and returns a generic message. Routes: `/health` (unauthenticated) and the `/v1` router, which applies `check_csrf` to every route; also `/v1/opportunities` and `/v1/import-status`. |
| `deps.py` | **The authorisation boundary.** `current_user` resolves the `tf_session` cookie and re-checks membership. `current_profile` is the active supplier, which always comes from the session. `check_csrf` requires `X-TenderFit` on non-GET requests. |
| `accounts.py` | `/v1/auth/*` and `/v1/profiles`, `/v1/profile`. |
| `assessments.py` | Lot assessment, documents and uploads, facts and evidence, questions, confirmations, traces, jobs, changes. |
| `screens.py` | Read models for screens: search, tender, lot summary, passage, document file, preparation, evidence impact, pursued lots. |
| `simulations.py` | Mode B over HTTP. It maps `SimulationNotFound`→404, `SimulationConflict`→409 and `SimulationError`→422, and every response carries `"hypothetical": true`. By design there is **no route that applies a simulation to live data**. |

### 2.3 Mode A domain

| Module | Responsibility | Key names |
|---|---|---|
| `ingest/` | Fetch OCDS releases from Find a Tender, store them raw, normalise them into opportunity, notice version, lot and buyer, and derive status | `client.FtsClient`, `importer.incremental`/`run_import`/`replay`/`destructive_rebuild`, `normalise.normalise`, `status.lot_status` |
| `documents/` | Fetch attachments (public addresses only, 25 MB cap, 5 redirects), parse PDF (PyMuPDF) and DOCX (python-docx), split into passages, find references, keep the per-version inventory, handle supplier uploads | `fetch.fetch`/`is_public_url`, `parse.parse_pdf`/`parse_docx`, `passages.build`, `references.find`, `service.process_version`/`inventory`/`attach_upload`/`inherit_from_previous`, `text.quote_ok` (`QUOTE_THRESHOLD = 95`) |
| `extraction/` | Structured LLM extraction of requirements and atomic conditions, then validation | `extract.extract_version`/`verify`, `schema.EXTRACTION_SCHEMA`/`CATEGORIES`, `validate.check_condition`/`same_digits`/`normalise_lot`/`ALLOWED_FIELDS` |
| `retrieval/` | Hybrid search, the typed passage graph, and VersionRAG paths | `search.search`/`index_version`/`corrective_query`, `graph.build_graph`/`expand`/`reference_gaps`, `versioned.resolve` |
| `rules/` | Deterministic condition evaluation and lot-level aggregation | `engine.evaluate` (`Condition` kinds `at_least`, `at_most`, `within_years`, `region_covered`, `has_evidence`, `min_amount`, `valid_through`), `aggregate.dimensions`/`eligibility`/`relevance` |
| `assessment/` | The per-lot pipeline, the guard, confirmations and overrides, and contradiction checks | `pipeline.assess_lot`/`decide`/`guard`/`_combine`/`parent_status`/`evidence_quality`/`recover_stale_assessments`, `config.PipelineConfig`/`BASELINES`, `confirm.confirm`/`overrides`/`POINTS` (G1–G7), `contradiction.find_conflicts` |
| `agent/loop.py` | The Mode A agent: for one escalated requirement, it reads the documents with 8 tools and finishes with `record_verdict` or `raise_question`. Its output goes through `guard()` again. | `run_agent`, `Budget`, `TOOLS`, `AGENT_VERSION = "agent-v1"` |
| `changes/` | Amendment detection, the classification and impact plan, targeted and full reassessment, shadow audits, profile-change reassessment | `diff.field_diff`/`align`/`classify`, `impact.plan_reassessment`/`load_deps`, `apply.process_pending`/`apply_version_change`/`shadow_full`/`apply_profile_change`/`AMENDMENT_LOCK` |
| `profile/` | Supplier profile and facts, evidence files, automatic evidence checks, the validity model, the daily expiry check | `service.add_fact`/`edit_fact`/`add_evidence`/`link_support`/`daily_check`, `verify.verify_fact_evidence`, `validity.check` |
| `eval/` | Experiment harness: arms B0–B5, metrics, the synthetic amendment regression | `run`, `metrics`, `regression` |

### 2.4 Mode B domain (`tenderfit/simulation/`)

| Module | Responsibility | Pure? | Writes |
|---|---|---|---|
| `changes.py` | The closed vocabulary of 9 hypothetical change types (pydantic models), `parse_change`/`dump_change`, and the error types `SimulationError`/`SimulationNotFound`/`SimulationConflict` | yes | — |
| `snapshot.py` | `Snapshot` (facts, requirements with conditions, tender fields, `as_of`), `take_snapshot`, `drift`, `sha256()`, `ENGINE_VERSION = "sim-v1"` | reads | — |
| `inputs.py` | Live inputs a run reads outside the snapshot (overrides, answers, requirements, documents, rejections, baseline_record): their digests and comparison | reads | — |
| `scenario.py` | `apply_changes(snapshot, changes)`: applies changes in memory and checks each against the state so far | yes | — |
| `impact.py` | `reach()`: which requirements a change can reach, from both recorded dependencies ("used") and what conditions can read ("examines") | yes | — |
| `compare.py` | `Row` (baseline, control and simulated status), `summarise()` | yes | — |
| `isolation.py` | `read_only(conn)`: READ ONLY + REPEATABLE READ, always rolled back; `require_idle` | — | — |
| `store.py` | **The only writer of simulation rows**: `create`, `create_variation`, `add_change`, `discard`, `get`, `list_for_profile`, `results`; `MAX_CHANGES = 25`, `MAX_CHANGE_BYTES = 4000` | — | `simulation`, `simulation_change` |
| `engine.py` | `run` (start → evaluate READ ONLY → record), `preview`, `close_abandoned_runs`, `recover_stale_simulations` | — | `simulation` status, `simulation_result` |
| `spend.py` | Rolling 24-hour spend per supplier, the advisory lock, worst-case call pricing, `admit_call`, `allowance`, `frees_at` | reads + lock | — |
| `agent.py` | The what-if agent: `begin` (in the API request), `investigate` (in the worker), `ask`, `answer`, `trace` | — | `simulation_agent_step` (via `_Session.record`), and the store/engine calls above |
| `scenarios.py` | Phase 4: builder inputs (`inputs_for_assessment`, `inputs_for_simulation`, `view`), `compare`, `direction`, `reach_by_change`, `priorities`, `_integrity` | reads | — |

**What must not depend on `tenderfit.simulation`:** every module outside it, except `api/simulations.py` and `jobs/worker.py`.

---

## 3. Mode A: the assessment lifecycle

### 3.1 From ingestion to a supplier-facing verdict

```mermaid
flowchart TD
  subgraph Ingest["Worker: import job, every IMPORT_INTERVAL_HOURS (4)"]
    I1[FtsClient fetches releases in 24 h windows<br/>with IMPORT_OVERLAP_MINUTES overlap] --> I2[store_raw: raw_release, text, immutable]
    I2 --> I3[normalise → notice_version immutable, versions numbered by view]
    I3 --> I4[opportunity · lot · buyer · linked_notice · status]
    I4 --> I5{New version behind a current assessment?}
    I5 -->|yes| I6[enqueue detect_changes]
  end

  subgraph Assess["Worker: assess job for lot × supplier"]
    A0[_begin: running row, last completed stays current] --> A1[documents.process_version<br/>fetch · parse · passages · inventory]
    A1 --> A2[index_version: MiniLM embeddings into passage.embedding]
    A2 --> A3[graph.build_graph: REFERENCES / DEFINED_BY edges]
    A3 --> A4[extract_version: LLM → requirements + conditions]
    A4 --> A5{validate: quote ≥ 95 match and same digits,<br/>lot known, condition traceable}
    A5 -->|fail| A5r[requirement_rejection]
    A5 -->|ok| A6[requirement + requirement_condition + dependency]
    A6 --> A7[load_facts: profile + lot confirmations]
    A7 --> A8[for each requirement: decide]
  end

  subgraph Decide["pipeline.decide per requirement"]
    D1[rules.evaluate each condition] --> D2{all mandatory conditions settled?}
    D2 -->|yes| D3[_from_rules: deterministic verdict]
    D2 -->|no| D4[_model_decision: hybrid search + graph links,<br/>corrective search for reference gaps,<br/>verdict_cache, llm.json verdict]
    D4 --> D5[guard: citations verified, facts current,<br/>NOT MET needs current contrary fact,<br/>MET on declared-only proof → needs confirmation]
    D5 --> D6[_combine: rules bind the model]
    D6 --> D7{escalated and agent enabled?}
    D7 -->|yes| D8[agent.loop.run_agent → guard again]
    D7 -->|no| D9
    D8 --> D9[_apply_gaps · _apply_conflicts:<br/>unread reference or conflicting documents → lower to needs confirmation]
    D3 --> D9
  end

  A8 --> D1
  D9 --> R1[verdict + verdict_citation + verdict_fact +<br/>verdict_condition + dependency + question]
  R1 --> R2[G6 override of NOT MET counts as needs_confirmation]
  R2 --> R3[aggregate.dimensions → recommendation]
  R3 --> R4[one transaction: old assessment superseded,<br/>new one done and current]
  R4 --> UI[GET /v1/lots/:id/assessment → Assessment page]
```

### 3.2 Verdict policy

The policy is enforced in `pipeline.guard`, `rules.engine` and `pipeline.parent_status`.

| Verdict | Only when |
|---|---|
| `met` | Every mandatory condition is satisfied by a current, compatible fact. Where proof is needed (category `certificate`, `insurance`, `standard` or `experience`, or `evidence_needed` is set), a declaration alone is not enough. |
| `not_met` | An explicit, **current** contrary fact exists. A stale contrary fact gives `needs_confirmation`. Missing evidence never gives `not_met`. |
| `needs_confirmation` | Support exists but must be confirmed, or the facts or documents disagree, or the verdict rests on declared facts where proof is required. Also used when an essential reference was not read. |
| `unknown` | Nothing in the profile or documents settles it. |

`parent_status`: any `not_met` → `not_met`; all `met` → `met`; else `needs_confirmation` if anything is `met` or `needs_confirmation`, else `unknown`.

`evidence_quality` records `strong`, `weak`, `contradictory`, `missing` or `ambiguous`. These are reasons, not probabilities. **No confidence percentage exists anywhere, by design.**

### 3.3 Human confirmation points

The points are defined in `assessment/confirm.py:POINTS`.

| Point | Target | Decisions |
|---|---|---|
| G1 | lot | `pursue`, `not_for_us` |
| G2 | verdict | `confirm_declaration` |
| G3 | verdict | `confirm_for_lot` |
| G4 | fact | `renewed`, `reconfirmed` |
| G5 | verdict | `clarification_reviewed`, `clarification_sent` |
| G6 | verdict | `override`, `accept_blocker` |
| G7 | document_ref | `none_issued` |

### 3.4 Pipeline configurations

These are defined in `assessment/config.py`, and each is reachable from a setting.

| Arm | rules + guard | corrective | graph | agent | Notes |
|---|---|---|---|---|---|
| B0 | ✗ | ✗ | ✗ | ✗ | Not the historic system; it turns today's checks off |
| B1 | ✓ | ✗ | ✗ | ✗ | |
| B2 | ✓ | ✓ | ✗ | ✗ | |
| B3 | ✓ | ✗ | ✓ | ✗ | |
| B4 | ✓ | ✗ | ✗ | ✓ | |
| B5 / B5-full | ✓ | ✓ | ✓ | ✓ | B5-full reassesses everything on an amendment |
| VersionRAG | ✓ | ✓ | ✓ | ✗ | Two-hop, snapshot-scoped paths; `VERSIONRAG_ENABLED` |

**The live default is not one of these arms.**

- With no arm given and VersionRAG off, `pipeline._run` (and `simulation.engine.default_config`) build `PipelineConfig("B3" if AGENT_ENABLED else "B2", agent=AGENT_ENABLED)`.
- That leaves every other flag at its default: rules, guard, hybrid, coverage, graph and corrective are all on, and targeted reassessment is on. With the default `AGENT_ENABLED=true`, this is effectively B5 under the label "B3".
- So the name recorded on an assessment does not tell you the flags. Read `config.py` and `pipeline._run` rather than trusting the label. This is a known naming wart; see [TEAM_HANDOFF.md §13](TEAM_HANDOFF.md#13-where-older-documents-disagree-with-the-code).

---

## 4. Hybrid retrieval

`retrieval/search.py:search()` runs one query through these steps:

```mermaid
flowchart LR
  Q[query = statement + quote] --> L["Lexical: to_tsquery OR of terms<br/>(stop words removed), ts_rank_cd,<br/>top pool=50"]
  Q --> V["Vector: all-MiniLM-L6-v2,<br/>pgvector cosine distance, top pool=50"]
  G["Graph links the requirement names<br/>(graph.expand, one hop, ≤6)"] --> F
  L --> F["Reciprocal Rank Fusion<br/>score += 1/(60 + rank)"]
  V --> F
  F --> RR["Cross-encoder rerank<br/>ms-marco-MiniLM-L-6-v2, top 20"]
  RR --> K["Top k=5<br/>+ every graph-linked passage"]
  K --> W["Each hit records why:<br/>keyword rank · similar meaning rank · refers to / defined term"]
```

**Access filter.** Every query is restricted to the public pack plus the supplier's own uploads: `d.owner_profile_id IS NULL OR d.owner_profile_id = profile`. Search, graph expansion and reference-gap checks all apply it.

**Why hybrid.** Tender language mixes exact tokens with paraphrase. Exact tokens include standard numbers like "BS EN 1729-2", money amounts and section labels like "Appendix C"; dense vectors blur these, while full-text search matches them. Paraphrase, such as "employers' liability" versus "EL insurance", is where vectors help. RRF combines the two rankings without calibrating scores against each other. The cross-encoder then reads the query and passage together, which is more accurate than either retriever alone. Graph links are added because a requirement that says "see Appendix C" cannot be judged without Appendix C, whatever its similarity score.

**Corrective retrieval** (`config.corrective`) handles references the first search left unresolved. `graph.reference_gaps` lists references with no passage to follow and no document on record, and a second search asks for them (`corrective_query`), at most `CORRECTIVE_MAX_ATTEMPTS` (1) times. A requirement whose reference stays unread cannot be `met` (`_apply_gaps`).

**`RETRIEVAL_MIN_SCORE` is unset on purpose.** No evaluated threshold exists.

**Known gaps** (from `docs/LIMITATIONS.md`, confirmed in code):

- Embeddings truncate at 256 tokens.
- Retrieval does not filter by lot.
- A passage records only its first page.
- There is no OCR.

---

## 5. Amendments and dependencies

```mermaid
stateDiagram-v2
  [*] --> detected: import finds a newer version behind current assessments
  detected --> processing: detect_changes job (AMENDMENT_LOCK advisory lock)
  processing --> applied: every lot × supplier pair finished
  processing --> failed: any pair failed (finished pairs stay finished)
  failed --> processing: automatic retry with backoff (MAX_EVENT_ATTEMPTS = 5) or Retry now
  failed --> superseded: a newer notice replaced the target
  detected --> superseded
```

**Dependencies.** Each verdict records `dependency` rows (`requirement_id`, `version_id`, `kind`, `target_type`, `target_id`, `recorded_by`, `verdict_id`):

- what extraction found: source, references, definitions, parameters;
- what the verdict used: `fact`, the rule parameter `field` (for example `notice.deadline`, `lot.quantity`), and the `passage` and `document` items it read.

A verdict that uses a dated fact always depends on `notice.deadline`, because validity is measured at the deadline.

**Plan.**

- `changes/diff.py` aligns passages and fields between versions. `classify()` marks structural changes (sections such as award criteria, selection or definitions, or a change in lot count) as needing full reassessment.
- `changes/impact.py:plan_reassessment` intersects the changed targets with one assessment's dependencies.
- Unaffected verdicts are carried over (`carry`, `pmap`). Reached ones are re-decided.
- Pursued lots (G1) also get a `shadow_full` audit. Disagreements in mandatory verdicts are stored as missed dependencies.

**Profile changes.** A fact edit, new evidence, a G4 confirmation or a daily expiry takes the same targeted path, through `apply_profile_change`.

Mode B reuses `changes.impact.load_deps` to explain reach in the supplier's terms (§6.3).

---

## 6. Mode B: simulations

### 6.1 Lifecycle

The lifecycle is enforced by the `simulation_guard()` trigger, migrations 019 and 021.

```mermaid
stateDiagram-v2
  [*] --> created: store.create / create_variation (snapshot frozen, live inputs digested)
  created --> created: add_change (only while created, at most 25)
  created --> running: engine.run start (guarded UPDATE, second run loses → 409)
  running --> completed: results + summary in one transaction
  running --> failed: error, or abandoned run closed (advisory lock free)
  created --> discarded
  completed --> discarded
  failed --> discarded
  discarded --> [*]: final, nothing more can be added
```

What a simulation was decided on cannot change: the trigger rejects any update to `profile_id`, `lot_id`, `baseline_assessment_id`, `version_id`, `as_of`, `snapshot`, `snapshot_sha256`, `drift`, `engine_version`, `created_by`, `created_at` or `parent_id`. Changes and results are immutable (`forbid_update`). Changes can be inserted only while the simulation is `created`, and results only while it is `running` (`simulation_child_guard`).

### 6.2 Frozen and live inputs

| | What | Where it lives | Change detection |
|---|---|---|---|
| **Frozen** | Supplier facts (read from the live profile **at creation**), requirement wording, conditions and baseline verdicts, tender context fields, `as_of` | `simulation.snapshot` (jsonb) | `snapshot_sha256`, rechecked before every run (`engine._evaluate`) |
| **Live, tracked** | `overrides` (confirmation), `answers` (question), `requirements` (requirement), `documents` (document_ref, document_ref_state, document, passage), `rejections` (requirement_rejection), `baseline_record` (assessment, verdict, dependency) | Live tables, read during the run | `inputs.read()` digests them at creation (into `drift.live_inputs_at_creation`) and at run time. `inputs.compare` and `since_baseline` turn differences into `input_drift`. |
| **Drift** (baseline → control) | Facts edited or confirmed since the baseline ran, a different `as_of`, a superseded baseline, the baseline not on the current version | `simulation.drift` | Reported as drift and never credited to the hypothesis |
| **Results** | Per requirement: `baseline_status`, `control_status`, `simulated_status`, `how` (`rule`, `needs_model`, `not_affected`), and `simulated` jsonb `{simulated, control, reached, effect, drift, notes, input_drift}` | `simulation_result` | — |

The effect of a hypothesis is **control → simulated**. Both are computed by the same rules on the same snapshot in the same transaction, so the only difference between them is the changes.

### 6.3 Run

```mermaid
sequenceDiagram
  participant C as Caller (API or agent)
  participant E as engine.run
  participant PG as Postgres
  C->>E: run(profile, sim)
  E->>PG: pg_advisory_lock(RUN_LOCK, sim) on this connection
  E->>PG: UPDATE simulation SET status='running' WHERE status='created' (commit)
  Note over E,PG: Phase 2: isolation.read_only — READ ONLY, REPEATABLE READ
  E->>PG: load snapshot, check sha256
  E->>E: control = apply_changes(snap, [])<br/>simulated = apply_changes(snap, changes)
  E->>E: impact.reach: used (recorded deps) ∪ examines (what conditions read)
  E->>PG: inputs.read (live digests), since_baseline
  loop each requirement
    E->>PG: pipeline.decide(interpret=False, NO_MODEL) for control
    alt reached (or selective=False)
      E->>PG: decide for simulated
    end
    E->>E: needs_model → None, anything but a rule verdict → SimulationError
  end
  E->>E: lot_dimensions for control and simulated, summarise
  E->>PG: rollback (nothing could have been written)
  Note over E,PG: Phase 3: one write transaction
  E->>PG: INSERT simulation_result rows, UPDATE status='completed', summary
  E->>PG: commit, pg_advisory_unlock
```

- **No model, no spend, no cache.** `decide(interpret=False)` never calls `LLM` and reads no `verdict_cache`. `NO_MODEL` raises if it is touched.
- **Selective evaluation.** By default, only reached requirements are recomputed. `selective=False` recomputes everything and raises if a requirement it would have skipped comes out different, which tests the dependency analysis.
- **Recovery.** A run whose process died loses its connection, so Postgres releases the advisory lock. `close_abandoned_runs` only closes runs whose lock it can take, so it never closes a live run. It runs before simulation API reads and in the worker loop (`recover_stale_simulations`, 30 minutes).

### 6.4 The nine change kinds

The kinds are defined in `simulation/changes.py`, and the database CHECK on `simulation_change.kind` lists the same nine.

| Kind | Payload | Rule-decidable? |
|---|---|---|
| `set_condition_bound` | `requirement_id`, `condition` (ordinal), `bound` | yes |
| `set_condition_match` | `requirement_id`, `condition`, `match` | yes |
| `set_fact_validity` | `fact_id`, `valid_until` | yes |
| `set_fact_evidence` | `fact_id`, `evidence_status` (`document_on_file`, `declared`, `missing`, `unknown`, `needs_update`) | yes |
| `remove_fact` | `fact_id` | yes |
| `set_fact_value` | `fact_id`, `key`, `value` | yes |
| `add_fact` | `attribute`, `label`, `value`, `evidence_status`, `valid_until`. Gets a negative id: -1, -2, … | yes |
| `set_notice_field` | `field` (`notice.deadline` or `lot.quantity`), `value` | yes |
| `set_requirement_text` | `requirement_id`, `statement`, `quote` | **no**: reported as `needs_model` |

Each change is validated twice: once for shape (`parse_change`), and once against the snapshot in order (`scenario.apply_changes`).

---

## 7. The what-if agent

### 7.1 End to end

```mermaid
sequenceDiagram
  actor U as Supplier
  participant W as web AskPanel
  participant A as API what_if
  participant PG as Postgres
  participant K as Worker
  participant G as simulation.agent
  participant M as Anthropic
  participant E as engine / store

  U->>W: question (≤2000 chars), optional as_of
  W->>A: POST /v1/assessments/{id}/what-if
  A->>PG: spend.supplier_lock(profile)
  A->>A: 409 if a question is already queued or running<br/>429 if frees_at(session $0.50) is set (daily $5.00)<br/>429 if queue ≥ 20
  A->>G: begin: store.create + step 1 "question"
  A->>PG: enqueue simulation_agent (max_attempts=1)
  A-->>W: 202 {simulation_id, job_id}
  loop poll every 2.5 s (10 s after 60 polls)
    W->>A: GET /simulations/{id}, /trace, /jobs/{job}
  end
  K->>PG: claim (simulation_agent claimed after all other ready jobs)
  K->>G: investigate(profile, sim, default_llm())
  loop ≤ 12 tool calls, ≤ 150 s
    G->>G: budget / tool-call / deadline checks (≥15 s left)
    G->>PG: spend.admit_call(worst-case cost): day and session caps
    G->>M: tool_turn(timeout = remaining − 5 s, max_retries=0, max_tokens=4096)
    M-->>G: tool calls
    G->>E: read_baseline / read_facts (READ ONLY) · add_change (store) · preview · run_simulation (engine.run)
    G->>PG: record step (own transaction): tool, input, output, tokens, cost
  end
  G->>G: finish accepted only if claimed changes == summary.changed
  G->>PG: record "finish" / "cannot_simulate" / "end"
  W->>A: GET results → hypothetical banner, answer, caveats, trace
```

### 7.2 Tools

All tools are deterministic code. The model only chooses which to call and with what arguments.

| Tool | Implementation | Writes |
|---|---|---|
| `read_baseline` | `_Session.read_baseline`, under `read_only`. Requirement text is wrapped with `untrusted("tender_passage", …)`. | — |
| `read_facts` | The snapshot's facts, optionally filtered by attribute | — |
| `add_change` | A JSON string, parsed, then `store.add_change`, with the same validation as a person's change | `simulation_change` |
| `preview` | `engine.preview` (reach and why) | — |
| `run_simulation` | `engine.run` (rules only) | `simulation_result`, status |
| `finish` | Accepted only if `changed` equals `summary["changed"]` exactly. Caveats about needs_model, drift and stale baselines are added by code. | step |
| `cannot_simulate` | Outcome `unsupported` (for example, price) | step |

**Outcomes.**

- `answered`
- `unsupported`
- `budget`: deadline, tool calls, session or daily spend.
- `agent_unavailable`: a provider error. It is recorded as an `llm_error` step and its cost is estimated at the worst case if the call may have been billed.
- `skipped`: the simulation was no longer waiting.

**A provider failure is not a failed simulation.** The simulation stays `created` (or `completed` if it already ran), the job finishes, and the answer explains that nothing was guessed. A job that raises fails once and is never retried (`max_attempts=1`), because a retry would spend again.

### 7.3 Deterministic or model

| Concern | Deterministic | Model |
|---|---|---|
| Interpreting the question as typed changes | — | ✓ (proposes) |
| Validating changes | ✓ `changes.py`, `scenario.py` | — |
| Reach, verdicts, recommendation | ✓ `impact`, `rules`, `aggregate` | — |
| Which verdicts changed | ✓ `summary.changed`; `finish` is checked against it | — |
| Prose answer | — | ✓ (bounded to 4000 chars; claims checked) |
| Caveats on needs_model, drift and stale baselines | ✓ added by `finish` | optional extra caveats |
| Budget, deadline, authorisation | ✓ | — |
| Evidence retrieval | **Not available to this agent.** It reads the snapshot only, with no passage search. | — |

### 7.4 The two agents compared

| | Mode A `agent/loop.py` | Mode B `simulation/agent.py` |
|---|---|---|
| Purpose | Settle one escalated requirement | Answer one what-if question |
| Tools | `search_passages`, `read_section`, `open_attachment`, `check_clarifications`, `read_profile`, `check_evidence`, `compare`, `record_verdict`, `raise_question` | See §7.2 |
| Limit | `AGENT_MAX_TOOL_CALLS` (8) and the lot `Budget` ($0.50, 400k tokens, 180 s) | 12 tool calls, $0.50 per session, $5.00 per rolling 24 h, 150 s deadline |
| Output | A verdict, re-guarded and bound by rules | Hypothetical answer, never a verdict |
| Trace | `agent_step`, `llm_call` | `simulation_agent_step` only |

---

## 8. Scenarios, variations and comparison

```mermaid
flowchart TD
  B[Completed assessment] -->|GET /lots/:id/simulation-inputs?assessment_id| BI[Builder inputs: requirements with editable conditions,<br/>facts with editable keys, fields, evidence statuses]
  BI --> C1[POST /simulations with changes ≤25<br/>atomic: all valid or nothing created]
  C1 --> S1[(Scenario 1: snapshot S, sha H)]
  S1 -->|POST /simulations/:id/run| R1[completed]
  S1 -->|GET /simulations/:id/inputs| VI[same builder view, from S]
  VI -->|POST /simulations/:id/variations| S2[(Scenario 2: parent_id = 1,<br/>copies S and H exactly,<br/>fresh live-input digests)]
  S2 --> R2[completed]
  B --> S3[(Scenario 3: new snapshot S′, sha H′)]
  S3 --> R3[completed]
  R1 & R2 & R3 -->|GET /lots/:id/simulations/compare?ids=1,2,3| CMP[compare]
  CMP --> INT["integrity: same_snapshot? same as_of?<br/>same engine? same live inputs at run?"]
  CMP --> GRID[requirement × scenario cells:<br/>control → simulated, direction]
  CMP --> HL["highlights: blockers_resolved · blockers_now_unknown ·<br/>blockers_introduced · uncertainties_resolved ·<br/>now_needs_confirmation · needs_model"]
  CMP --> PR["priorities: widest_reach · addresses_blockers ·<br/>resolves_uncertainty · introduces_blockers ·<br/>little_or_no_effect · depends_on_evidence_not_held"]
```

- **Variation constraint** (migration 021). `simulation_parent_fkey` is the foreign key `(parent_id, profile_id, baseline_assessment_id, snapshot_sha256)` → `simulation (id, profile_id, baseline_assessment_id, snapshot_sha256)`. A child must therefore belong to the same supplier, have the same baseline and hold a byte-identical snapshot. `parent_id` is immutable. A discarded parent cannot be varied (409).
- **Direction.** Verdicts are ranked `not_met` < `unknown` < `needs_confirmation` < `met`. `needs_model` is `undetermined`. Results are transition counts by name; there is no score.
- **`blockers_resolved`** means mandatory `not_met` → `met` or `needs_confirmation`. A move from `not_met` to `unknown` is counted separately as `blockers_now_unknown`, not as resolved.
- **Attribution.** A moved requirement is `attributable` to a change only if no other change in that scenario reached it. Otherwise it is `shared_with_other_changes`, and nothing claims which change caused it.
- **Limits.**
  - 2–4 scenarios per comparison (`MAX_COMPARED`), all the caller's, all completed, all from one lot.
  - Ids are validated as 0 < id < 2⁶³.

---

## 9. Jobs and the worker

```mermaid
stateDiagram-v2
  [*] --> queued: enqueue (max_attempts default 5, simulation_agent 1)
  queued --> running: claim (FOR UPDATE SKIP LOCKED, lock_key free, run_after ≤ now)
  running --> done: complete
  running --> queued: fail with attempts left (backoff min(30·2^attempts, 3600) s)
  running --> failed: fail on last attempt
  running --> queued: stale reclaim (locked_at older than 1 h, simulation_agent 10 min) with attempts left
  running --> failed: stale reclaim with no attempts left
```

| Kind | Enqueued by | Handler | Lock key |
|---|---|---|---|
| `import` | `schedule_import` (every `IMPORT_INTERVAL_HOURS`, when `IMPORT_ENABLED`) | `run_import_job` | `import` |
| `detect_changes` | Import, or `schedule_changes` (spaced 5 min, respects backoff) | `run_detect_changes` → `changes.apply.process_pending` | `changes` |
| `assess` | `POST /lots/{id}/assess` (`enqueue_assess`, reuses a queued job for the same pair) | `assess_lot` | `lot:{id}` |
| `profile_change` | Fact edits and evidence; `daily_check` | `apply_profile_change` | `lot:{id}` |
| `shadow_full` | Targeted reassessment of a pursued lot | `changes.apply.shadow_full` | `lot:{id}` |
| `daily_check` | `schedule_daily_check` (once a day) | `profile.service.daily_check` | — |
| `simulation_agent` | `POST /assessments/{id}/what-if` | `run_simulation_agent` | — (one per supplier, enforced at admission) |

`LOW_PRIORITY = ("simulation_agent",)`: such jobs are claimed after every ready job of any other kind, so authoritative work never waits behind questions. `waiting_behind(job)` gives the queue position shown in the UI.

There is a single worker process, which runs jobs one at a time. `run_once` also schedules recurring work and recovers stale assessments (90 minutes) and simulations (30 minutes).

---

## 10. Database

There are 21 forward-only migrations, `migrations/001_ingest.sql` to `021_simulation_variations.sql`, applied by `db.migrate()` at API and worker start, or with `python -m tenderfit.db migrate`.

### 10.1 Tables by domain

| Domain | Tables (migration) |
|---|---|
| Ingest | `raw_release` (001, immutable), `buyer`, `opportunity`, `notice_version` (immutable), view `notice_version_numbered`, `lot`, `linked_notice`, `import_state`, `import_run` |
| Jobs | `job` (001) |
| Documents | `document` (002; `owner_profile_id` 014, page status 018), `document_ref` (002), `document_ref_state` (014, per supplier), `passage` (002; `tsv` and `embedding`), `passage_edge` (015) |
| Requirements | `requirement` (002/005/013; `owner_profile_id` 014, `carried_from` 008), `requirement_condition` (013), `requirement_rejection` (005), `dependency` (002; `verdict_id` 006) |
| Supplier | `business_profile`, `profile_fact`, `evidence_file`, `fact_support` (002/004/016), `evidence_page` (004), `fact_verification` (016) |
| Assessment | `assessment` (002/006/011; `shadow` 008), `verdict`, `verdict_citation`, `verdict_fact`, `verdict_condition` (013), `verdict_cache` (006), `question` (002/007/014), `confirmation` (002/007), `lot_fact_confirmation` (006/014), `lot_decision` (006), `agent_step` (009), `llm_call` (005) |
| Change processing | `change_event` (008/012), `change_impact` (008/012), `version_preparation` (012) |
| Accounts | `app_user`, `profile_member`, `user_session`, `login_failure` (017) |
| Mode B | `simulation` (019; `parent_id` 021), `simulation_change` (019), `simulation_result` (019), `simulation_agent_step` (020) |

### 10.2 Core relationships

```mermaid
erDiagram
  opportunity ||--o{ notice_version : "versions (immutable)"
  opportunity ||--o{ lot : has
  notice_version ||--o{ document : has
  notice_version ||--o{ document_ref : lists
  document ||--o{ passage : "splits into"
  passage ||--o{ passage_edge : "REFERENCES / DEFINED_BY"
  notice_version ||--o{ requirement : yields
  requirement ||--o{ requirement_condition : "atomic conditions"
  requirement ||--o{ dependency : "depends on"
  business_profile ||--o{ profile_fact : has
  business_profile ||--o{ evidence_file : owns
  profile_fact ||--o{ fact_support : "proven by"
  profile_fact ||--o{ fact_verification : "checked by"
  business_profile ||--o{ profile_member : "members"
  app_user ||--o{ profile_member : "belongs to"
  app_user ||--o{ user_session : has
  assessment }o--|| lot : "for"
  assessment }o--|| business_profile : "for"
  assessment ||--o{ verdict : contains
  verdict ||--o{ verdict_citation : cites
  verdict ||--o{ verdict_fact : uses
  verdict ||--o{ verdict_condition : details
  verdict ||--o{ question : asks
  change_event ||--o{ change_impact : causes
  document_ref ||--o{ document_ref_state : "per supplier"
```

### 10.3 Simulation relationships

```mermaid
erDiagram
  assessment ||--o{ simulation : "baseline (FK on id, profile_id, lot_id, version_id)"
  simulation ||--o{ simulation : "parent_id (FK on id, profile_id, baseline, snapshot_sha256)"
  simulation ||--o{ simulation_change : "ordinal 1..25, append-only while created"
  simulation ||--o{ simulation_result : "one per requirement, insert-only while running"
  simulation ||--o{ simulation_agent_step : "trace + cost, insert-only unless discarded"
  requirement ||--o{ simulation_result : "requirement_id"
  verdict ||--o{ simulation_result : "baseline_verdict_id"
  simulation {
    bigint id
    bigint profile_id
    bigint lot_id
    bigint baseline_assessment_id
    bigint version_id
    bigint parent_id
    text status
    date as_of
    jsonb snapshot
    text snapshot_sha256
    jsonb drift
    text engine_version
    jsonb summary
  }
```

Simulation tables point at live tables, and never the other way round. No live table gained a column for Mode B.

### 10.4 Constraints worth knowing

| Constraint | Effect |
|---|---|
| `assessment_one_current`, a unique partial index on `(lot_id, profile_id) WHERE status='done' AND NOT superseded AND NOT shadow` | Exactly one current assessment per lot and supplier |
| `assessment_one_running` | At most one running non-shadow run per lot and supplier |
| `assessment_baseline_key` + the simulation FK | A simulation's baseline belongs to the supplier, lot and version it names |
| `simulation_lineage_key` + `simulation_parent_fkey` | A variation shares its parent's supplier, baseline and exact snapshot |
| `simulation_guard`, `simulation_child_guard`, `simulation_agent_step_open`, `forbid_update` | Lifecycle, immutability, append windows |
| `notice_version_immutable`, `raw_release_immutable` | Source data is never edited |
| `change_event_version` (unique per version pair), `change_impact_once` | No duplicate amendment processing |

---

## 11. API surface

All routes except `/health` are under `/v1` and need a session cookie (`tf_session`). Non-GET requests also need the `X-TenderFit: 1` header. The supplier always comes from the session. The OpenAPI document is `docs/openapi.json`, regenerated by `scripts/export_openapi.py` and checked by `tests/test_api_contract.py`.

| Group | Routes |
|---|---|
| Health | `GET /health` |
| Auth | `GET /v1/auth/config`, `POST /v1/auth/register`, `POST /v1/auth/login`, `POST /v1/auth/logout`, `GET /v1/auth/me`, `POST /v1/auth/active-profile` |
| Profiles | `GET/POST /v1/profiles`, `PUT /v1/profile`, `GET/POST /v1/profile/facts`, `PUT /v1/profile/facts/{id}`, `POST …/renew`, `POST …/verify`, `POST /v1/profile/evidence`, `GET /v1/profile/evidence-impact`, `GET /v1/profile/pursued` |
| Tenders | `GET /v1/opportunities`, `POST /v1/search`, `GET /v1/tenders/{id}`, `GET /v1/lots/{id}`, `GET /v1/import-status` |
| Assessment | `POST /v1/lots/{id}/assess`, `GET /v1/lots/{id}/assessment`, `GET/POST /v1/lots/{id}/documents`, `GET /v1/lots/{id}/preparation`, `GET /v1/passages/{id}`, `GET /v1/documents/{id}/file`, `POST /v1/questions/{id}/answer`, `POST /v1/confirmations`, `GET /v1/assessments/{id}/trace`, `GET /v1/jobs/{id}` |
| Changes | `GET /v1/changes`, `POST /v1/changes/{id}/retry` |
| Simulations | `GET /v1/lots/{id}/simulations`, `POST /v1/simulations`, `GET /v1/simulations/{id}`, `POST …/changes`, `GET …/preview`, `POST …/run`, `GET …/results`, `POST …/discard`, `POST …/variations`, `GET …/inputs`, `GET …/trace`, `GET /v1/lots/{id}/simulation-inputs?assessment_id=`, `GET /v1/lots/{id}/simulations/compare?ids=` |
| What-if agent | `POST /v1/assessments/{id}/what-if` (202), `GET /v1/what-if/allowance` |

Status codes follow one convention:

- **404**: unknown, or another supplier's resource. The caller cannot tell which.
- **409**: valid, but not allowed in the current state.
- **422**: invalid input.
- **429**: spend or queue limit, with `Retry-After`.

---

## 12. Frontend

`web/src` uses React 18.3, react-router-dom 6.30 and Vite 5, with no UI framework; styles live in `styles.css`. During development, `/api` is proxied to `API_URL` (default `http://localhost:8000`) by `vite.config.ts`. In Compose, nginx proxies it.

| Route | Component | Mode |
|---|---|---|
| `/opportunities` | `pages/Opportunities.tsx` | A |
| `/tenders/:tenderId` | `pages/Tender.tsx` | A |
| `/lots/:lotId` | `pages/Assessment.tsx`, with tabs Requirements · Documents · What-ifs | A, plus the B entry point |
| `/lots/:lotId/simulations/:simId` | `whatif/SimulationPage.tsx` | B |
| `/lots/:lotId/scenarios/new?assessment=…` or `?from=…` | `whatif/ScenarioBuilder.tsx` | B |
| `/lots/:lotId/compare?ids=…` | `whatif/ComparePage.tsx` | B |
| `/changes`, `/profile`, `/preparation`, `/lots/:lotId/preparation` | `pages/*` | A |

The Mode B client is `whatif/api.ts`, with types `Inputs`, `Comparison` and `ChangeReach`, and `MAX_COMPARED`. Polling lives in `whatif/useInvestigation.ts` (`POLL_MS` 2500, `SLOW_POLL_MS` 10000 after 60 polls). Labels come from `whatif/describe.ts`. Hypothetical verdicts render as dashed, labelled chips, never the plain verdict pill (`SimulationPage.tsx`), under `HypotheticalBanner`. `VITE_STATIC=1` builds a read-only demo from a snapshot JSON (`web/src/api.ts`).

---

## 13. Invariants and where they are enforced

| # | Invariant | Enforced by | Tested in |
|---|---|---|---|
| I1 | Mode B never writes a live table | `isolation.read_only` (Postgres refuses writes), `store` as the only writer, import allow-list | `test_simulation.py`, `test_simulation_api.py`, `test_simulation_scenarios.py` (live fingerprint unchanged) |
| I2 | What a simulation was decided on never changes | `simulation_guard`, `forbid_update`, `snapshot_sha256` recheck | `test_simulation.py` |
| I3 | Control and simulated differ only by the changes | Same snapshot, same rules, one REPEATABLE READ transaction; drift reported separately | `test_simulation_engine.py`, `test_simulation_inputs.py` |
| I4 | A skipped requirement could not have changed | `impact.reach`; `selective=False` raises on disagreement | `test_simulation_impact.py` |
| I5 | Supplier isolation: no route takes a supplier from the client; another supplier's resource is 404 | `deps.current_profile`, `owner_profile_id` filters, store scoping | `test_isolation.py`, `test_auth.py`, `test_simulation_api.py` |
| I6 | A model never decides alone: rules bind it, citations must exist, missing evidence never gives NOT MET | `guard`, `_combine`, `rules.engine` | `test_pipeline.py`, `test_rules_regression.py` (property tests), `test_soundness.py` |
| I7 | A number the model invents never becomes a rule input | `extraction/validate.py` | `test_extraction_validation.py` |
| I8 | A failure leaves the last good assessment current | `_begin` / `_run` single commit, `assessment_one_current` | `test_assessment_lifecycle.py` |
| I9 | The what-if agent's spend is bounded before each call | `spend.admit_call` worst case, deadline, `max_retries=0`, `max_attempts=1` | `test_simulation_agent_jobs.py` |
| I10 | Agent answers cannot misstate results | `_Session.finish` compares claims to `summary.changed` | `test_simulation_agent.py` |
| I11 | Authoritative jobs never wait behind what-if jobs | `queue.LOW_PRIORITY` claim order | `test_simulation_agent_jobs.py` |
| I12 | An abandoned run is closed, and a live one never is | Session advisory lock + `close_abandoned_runs` | `test_simulation_recovery.py` |
| I13 | API contract and code agree | `docs/openapi.json` + `test_api_contract.py`; route coverage guards in `test_auth.py`, `test_simulation_api.py` | same |

---

## 14. Configuration reference

The settings live in `tenderfit/config.py`. Environment variables match the field names in upper case.

| Setting | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | **Secret.** Needed for real extraction, verdicts and agents. |
| `DATABASE_URL` | `postgresql://tenderfit:tenderfit@localhost:5432/tenderfit` | A placeholder default; Compose overrides it |
| `FTS_BASE_URL`, `IMPORT_ENABLED`, `IMPORT_INTERVAL_HOURS`, `IMPORT_OVERLAP_MINUTES`, `IMPORT_CHUNK_HOURS`, `STATUS_FRESH_HOURS`, `HTTP_MAX_RETRIES` | FTS 1.0 API, true, 4, 60, 24, 24, 8 | Import |
| `DOCUMENT_DIR` | `data/documents` | Document storage |
| `LLM_MODEL_EXTRACT`, `LLM_MODEL_AGENT`, `LLM_EFFORT`, `LLM_TIMEOUT_S`, `LLM_RECORD_PATH` | `claude-opus-5-5`, `claude-opus-5-5`, `medium`, 300, unset | `extract` and `verdict` use the extract model |
| `DEMO_SCRIPTED_LLM` | false | Demo only |
| `LOT_COST_CAP_USD`, `LOT_TOKEN_CAP`, `LOT_TIME_CAP_S`, `AGENT_ENABLED`, `AGENT_MAX_TOOL_CALLS` | 0.50, 400000, 180, true, 8 | Mode A budget per lot |
| `SIMULATION_AGENT_DAILY_USD`, `_SESSION_USD`, `_DEADLINE_S`, `_MAX_TOKENS`, `_QUEUE_MAX` | 5.00, 0.50, 150, 4096, 20 | Mode B agent, read at each check |
| `VERSIONRAG_ENABLED`, `RETRIEVAL_MIN_SCORE`, `CORRECTIVE_MAX_ATTEMPTS` | false, unset, 1 | Retrieval |
| `EMBEDDING_MODEL`, `RERANK_MODEL` | `sentence-transformers/all-MiniLM-L6-v2`, `cross-encoder/ms-marco-MiniLM-L-6-v2` | Downloaded on first use |
| `MAX_UPLOAD_BYTES` | 26214400 (25 MB) | |
| `REGISTRATION_ENABLED`, `SESSION_HOURS`, `SESSION_COOKIE_SECURE`, `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_MINUTES` | false, 12, true, 10, 15 | Accounts |

Compose-only variables (`.env.example`): `POSTGRES_USER`, `POSTGRES_PASSWORD` (**required**), `POSTGRES_DB`, `DB_PORT` (5433), `WEB_PORT` (8080).

Model prices used for cost accounting (`llm.PRICES`, USD per million tokens of input, output and cache read): opus 4.00/20.00/0.20, sonnet 2.00/10.00/0.20, haiku 0.10/0.50/0.01. For worst-case accounting, `spend._prices` treats an unpriced model as the most expensive one.

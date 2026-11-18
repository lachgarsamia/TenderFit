# Mode B API and frontend: approved plan

Status: **plan approved with refinements (§11). Phase 1 (deterministic API) implemented on `feature/simulation-api`; see §12.
Phases 2–4 not yet authorised.** Base: `main` at `431541c`.

Goal: expose the existing what-if functionality (`tenderfit/simulation/`) through the API and the web app, without
rewriting the engine, without new infrastructure, and with hypothetical results clearly separate from real ones.

---

## 0. What the repository already gives us

| Concern | Existing convention | Where |
|---|---|---|
| Routing | `APIRouter(dependencies=[Depends(current_user)])`, mounted under `/v1`, which adds `check_csrf` (an `X-TenderFit` header on every non-GET) | `api/main.py`, `api/deps.py` |
| Acting supplier | `current_profile` comes from the session, never from the request; every query is scoped by it, and another supplier's id is a **404** | `api/deps.py`, e.g. `_own_fact` |
| Errors | `HTTPException` 404 / 409 / 422, and the message is the user-facing text | `api/assessments.py` |
| Background work | the `job` table plus `queue.enqueue` / `enqueue_unique` / `lock_key`; **one** worker process running jobs **sequentially**; up to 5 attempts with exponential backoff; stale runs reclaimed after 1 h; claim order is `run_after, id` with no priorities | `jobs/queue.py`, `jobs/worker.py`, `docker-compose.yml` |
| Job polling | `GET /jobs/{id}` (scoped by `payload.profile_id`) and the frontend's `waitForJobs()`, which polls every 1.5 s | `api/assessments.py`, `web/src/api.ts` |
| Long-running UI | `ProgressBanner` | `web/src/components/ProgressBanner.tsx` |
| API server | a single `uvicorn` process with sync `def` endpoints (thread pool); nginx `proxy_read_timeout 300s` | `docker-compose.yml`, `web/nginx.conf` |
| Model client | the Anthropic client is built once with `max_retries=4` and `timeout=llm_timeout_s` (300 s); `tool_turn` uses `max_tokens=16000`. `tool_turn` is implemented by `AnthropicLLM`, `RecordedLLM` and `DemoLLM`, plus the test fakes | `tenderfit/llm.py`, `tenderfit/demo/llm.py`, `tests/fakes.py` |
| Read-only demo | `VITE_STATIC=1` serves GETs from a snapshot, and every non-GET throws | `web/src/api.ts` |
| Simulation functions | `store.create / add_change / discard / get / list_for_profile / results`, `engine.preview / run / recover_stale_simulations`, `agent.ask / answer / trace`. Each takes `profile_id` and owns its transaction | `tenderfit/simulation/` |

Measured on the 4-requirement fixture (median of 5 runs): `create` 5 ms, `add_change` 2 ms, `preview` 2 ms, a full
rules-only `run` 14 ms. The agent took **about 12 s per answered question** with the real model (`claude-opus-5-5`):
6 tool turns, about $0.06–0.08. Real lots have 20–80 requirements, so a run is still well under a second, while an
agent session grows with the size of the baseline sent to the model.

**What's wrong with the limits as they stand:** `Budget.time_cap` (180 s) is checked only *between* turns, and a single
model call can take up to 300 s × 5 attempts, about 25 minutes. Phase 2 replaces this with the single deadline in §2.2.

---

## 1. Architecture

```
 Browser ──► /v1 router (session, CSRF) ──► api/simulations.py  (new, the only API module that imports tenderfit.simulation)
                                              │
            deterministic, synchronous:       ├─► store.create / add_change / discard / get / results
            milliseconds, no model, no cost   ├─► engine.preview / engine.run
                                              │
            agent, asynchronous:              └─► spend admission (§4.2), then store.create
                                                  and queue.enqueue("simulation_agent", max_attempts=1), in one transaction
                                                                   │
 worker (existing process) ── run_once ── claim (non-agent jobs first, §2.3) ── HANDLERS["simulation_agent"]
                            │                                         └─► agent.investigate(simulation_id, question)
                            └─ also calls engine.recover_stale_simulations()      (150 s deadline; per-call spend check)
```

- **One new router file**, `tenderfit/api/simulations.py`, included in `api/main.py` next to the others. Mode A's
  routers don't change.
- **One new job kind**, `simulation_agent`, in the existing worker. No new service, queue, broker or process.
- **No new tables.** Migrations 019 and 020 already hold everything. No migration is expected.
- **Small, additive changes to shared infrastructure.** Each keeps current behaviour when the new argument isn't
  passed, and **the existing worker, queue and LLM tests must pass unedited**:
  - `queue.enqueue(..., max_attempts=None)`: `None` keeps the table default of 5;
  - `queue.claim()`: non-agent kinds are ordered first (§2.3). For queues without agent jobs, the order is unchanged;
  - `LLM.tool_turn(..., *, timeout=None, max_retries=None, max_tokens=None)`, on the protocol, `AnthropicLLM`,
    `RecordedLLM` and `DemoLLM`: `None` keeps today's values (300 s, 4 retries, 16000 tokens). Only the simulation agent
    passes them. Mode A never does. The Mode A test fake (`tests/fakes.py`) is never handed these arguments, so it stays
    unedited;
  - `GET /jobs/{id}` gains a `waiting_behind` field, for agent jobs only (§2.3);
  - `worker.run_once`: one handler and one `recover_stale_simulations()` call;
  - `api/main.py`: `include_router`.
- **Inside `tenderfit/simulation`:**
  - a `lot_id` filter for `store.list_for_profile`;
  - a `SimulationConflict(SimulationError)` subclass, so lifecycle errors map to 409;
  - `agent.investigate()`: the existing `ask()` loop, split into "create" and "investigate" so the API can return the
    simulation id before the model runs;
  - the deadline and per-call spend checks (§2.2, §4.2);
  - an `llm_error` trace step (§4.1).

  The decision logic of the loop and the engine are unchanged.

---

## 2. Execution model

### 2.1 Synchronous versus background

| Operation | Choice | Why |
|---|---|---|
| create, add change, preview, run, discard, reads | **synchronous** | 2–14 ms measured, rules only, deterministic, free. A job would add 1.5 s of polling latency and queue contention for no benefit. |
| agent investigation | **background job** in the existing worker, `max_attempts = 1`, no automatic retries | A session runs for tens of seconds and up to the 150 s deadline. Inside a request it would hold an API thread and a DB connection, and a slow proxy path could turn into a 504 while the session kept running unseen on the server. Refreshing would then start a second paid session. Assessments already run in the worker, and the frontend's `waitForJobs` + `ProgressBanner` pattern applies unchanged. |

**Retries:** an investigation is paid and non-idempotent: its changes and trace are already committed. An automatic
retry would duplicate cost or collide with the existing trace, so the job is enqueued with `max_attempts = 1`. The agent
already converts model outages into an `agent_unavailable` outcome, so the job *succeeds* in that case. Only an
infrastructure fault (DB lost, worker killed) fails the job, and the user is then offered "Ask again", which creates a
new simulation. Rules-only runs need no retry: they're milliseconds long and fail atomically.

### 2.2 One coherent execution budget

A session has a **hard 150-second deadline** (`simulation_agent_deadline_s = 150`), measured from the moment the worker
starts the job. Every limit derives from it:

| Limit | Value | Enforced where |
|---|---|---|
| Session deadline | 150 s | `Budget(time_cap=deadline)`, checked before every turn |
| Per-call timeout | `min(remaining − 5 s safety margin, llm_timeout_s)` | passed as `timeout=` to `tool_turn`; no call can outlive the session |
| Minimum useful call | if less than 15 s remain, the session ends `budget` without starting a call | before every turn |
| SDK retries | **0** for simulation-agent calls (`max_retries=0`) | passed to `tool_turn`. A failure ends the session as `agent_unavailable`, which is visible and not guessed. |
| Output per call | `max_tokens = 4096` for agent turns (`simulation_agent_max_tokens`) | passed to `tool_turn`. It bounds the worst-case cost of one call for the spend check (§4.2), and is ample for one tool call. |
| Tool calls | 12 | unchanged |
| Session spend | `simulation_agent_session_usd = 0.50` | the session `Budget.cost_cap`, plus the per-call check (§4.2) |

**Worst case:** about 150 s plus one 5 s margin. The previous worst case was about 25 minutes. This is what bounds how
long an assessment can wait behind one agent session (§2.3).

Mode A's agent and verdict calls keep their current timeout (300 s), retries (4) and `max_tokens` (16000), because they
never pass the new arguments.

### 2.3 Sharing the single worker: priority, limits, visibility

Assessments are the authoritative work and **must never wait behind a backlog of agent questions**. Only the existing
`job` table and the one worker are used.

- **Priority:** `queue.claim()` orders claimable jobs by `(kind = 'simulation_agent'), run_after, id`, so any ready
  non-agent job is claimed before any agent job. The existing `lock_key` and `FOR UPDATE SKIP LOCKED` rules are unchanged.
  - **Guarantee:** a ready assessment waits for at most **one** agent session already in progress, which is bounded at
    about 155 s by §2.2, and never for queued ones.
- **Execution limit:** the deadline in §2.2. The worker's stale-job reclaim (1 h) remains the backstop for a dead worker.
- **Supplier concurrency:** at most **one** queued or running `simulation_agent` job per supplier. A second question
  returns **409** with the existing job's `simulation_id` and `job_id`, so the client can attach to it. This also means a
  repeated click never starts a second session.
- **Global queue ceiling:** at most **20** queued `simulation_agent` jobs in total (`simulation_agent_queue_max`). Above
  that, the what-if endpoint returns **429** ("TenderFit is busy; try again shortly"), with `Retry-After: 30`.
- **Visibility:** for `simulation_agent` jobs, `GET /jobs/{id}` adds `waiting_behind`: the number of queued or running
  jobs the worker will take before this one under the ordering above. The UI shows "Waiting behind N tasks" until the
  job starts, then "Investigating…".
- **The accepted trade-off:** under sustained assessment load, agent questions wait longer, and the UI says so. If that
  becomes a problem, the remedy is configuration: a second worker replica, or one restricted to agent jobs. It isn't
  part of this plan.

---

## 3. Endpoint specification

All are under `/v1`, require a session, and non-GETs require `X-TenderFit`. The supplier is always `current_profile`.
Every response that carries simulation data includes `"hypothetical": true` and the notice string.

| Method and path | Purpose | Body / query | Success | Errors |
|---|---|---|---|---|
| `GET /lots/{lot_id}/simulations` | list this supplier's simulations for a lot | `include_discarded=false`, `limit` ≤ 200 | 200 `[{id, title, status, baseline_assessment_id, as_of, created_by, created_at, changes, agent: {job_id, outcome}?}]` | 404 unknown lot |
| `POST /simulations` | create a manual simulation from one of this supplier's assessments | `{assessment_id, title?, as_of?}` | 201 detail | 404 not this supplier's; 409 not a completed, non-shadow assessment |
| `GET /simulations/{id}` | detail | | 200 `{id, status, title, baseline: {assessment_id, version_id, recommendation, superseded}, as_of, snapshot_sha256, engine_version, drift, changes[], summary?, error?, answer?, agent_job?}`. The snapshot itself is left out: it's large, and the supplier's facts are served by `/profile`. | 404 |
| `POST /simulations/{id}/changes` | add one hypothetical change, any of the **nine** supported kinds | `{change: {...}}`, the same JSON as `simulation.changes` | 201 `{ordinal, fact_id?}` | 404; 409 not `created`; 422 invalid (the message names the change and field) |
| `GET /simulations/{id}/preview` | which requirements the changes can reach, and why | | 200 `{reached: [{requirement_id, statement, why: [{kind, target, detail}]}], not_reached: [...]}` | 404 |
| `POST /simulations/{id}/run` | run the rules (sync) | | 200 detail with `summary` | 404; 409 already run / discarded; 500 marks the run `failed` (the engine does this already) |
| `GET /simulations/{id}/results` | per-requirement comparison | | 200 `[{requirement_id, statement, obligation, baseline_status, control_status, simulated_status, how, effect, drift, reached[], reason_control, reason_simulated, notes[]}]` | 404; 409 not completed |
| `POST /simulations/{id}/discard` | soft, final | | 200 detail | 404; 409 running or already discarded |
| `POST /assessments/{assessment_id}/what-if` | ask the agent | `{question}` (1–2000 chars) | **202** `{simulation_id, job_id}` | 404; 409 this supplier already has an investigation queued or running (returns its ids); 422 empty or too long; 429 daily spend allowance insufficient (§4.2) or global queue full (§2.3), with `Retry-After` |
| `GET /simulations/{id}/trace` | the agent's steps | | 200 `[{step, tool, input, output, model, tokens, cost_usd, cost_estimated, created_at}]`; tender text in outputs is already wrapped as untrusted | 404 |

**Job polling** reuses `GET /jobs/{job_id}`, since the payload carries `profile_id`. For agent jobs it adds
`waiting_behind` (§2.3).

**Status codes:**
- `SimulationNotFound` maps to **404**. Another supplier's simulation is indistinguishable from a missing one.
- Validation errors map to **422**.
- Lifecycle errors (`SimulationConflict`) map to **409**.

**Deliberately not provided:**
- no `PUT`/`DELETE` on changes, because a simulation's hypothesis is append-only until it runs;
- no hard delete;
- **no promotion or "apply" endpoint, and no "Make this real" link in the UI** (§7, §11.7).

---

## 4. Security and spend control

### 4.1 Authorization and isolation

- **Authentication and CSRF:** inherited from the `/v1` router (`check_csrf`) and the router-level `current_user`.
- **Supplier ownership:** `profile_id` comes only from `current_profile`. Every simulation function is already
  scoped by it, and the composite FK in 019 guarantees in the database that a simulation's baseline belongs to the same
  supplier, lot and version. Body fields never carry a `profile_id`, and fact or requirement ids in a change are
  re-validated against that supplier's snapshot.
- **Lot visibility:** `GET /lots/{lot_id}/simulations` checks the lot exists, as `_lot()` does, then lists only this
  supplier's rows.
- **Job visibility:** `simulation_agent` payloads carry `profile_id`, so `GET /jobs/{id}` stays supplier-scoped.
  `waiting_behind` is a count only; it never reveals other suppliers' jobs.
- **Prompt injection:** the question is the user's own, and it goes only into that user's simulation. Tender text
  reaches the model wrapped as untrusted (already tested). The model has no tool that writes outside the simulation.

### 4.2 Spend accounting and admission

**Settings.** All are in `tenderfit/config.py`, read at the time of each check, and none is hard-coded at a call site:

| Setting | Initial default | Meaning |
|---|---|---|
| `simulation_agent_daily_usd` | **5.00** | a supplier's maximum agent spend over any rolling 24 h |
| `simulation_agent_session_usd` | **0.50** | the allowance a session reserves at admission, and its cost cap |
| `simulation_agent_deadline_s` | 150 | §2.2 |
| `simulation_agent_max_tokens` | 4096 | §2.2 |
| `simulation_agent_queue_max` | 20 | §2.3 |

**What counts as spend.** A supplier's spend is the sum of `simulation_agent_step.cost_usd` over the last 24 hours
(`created_at > now() − interval '24 hours'`), across all of that supplier's simulations. Every model call made by an
agent session is recorded as a step, whatever its outcome:

| Call outcome | Step recorded | `cost_usd` |
|---|---|---|
| Completed | the tool step(s), as today; the turn's cost goes on its first step | **actual**, from the response's usage |
| Failed or timed out with no usage returned | a new `llm_error` step (`output.error`, `output.cost_estimated = true`) | **estimated, conservatively.** Input tokens ≈ the size of the serialised request (system + messages + tools) ÷ 3 characters per token, priced at the model's input rate, plus `max_tokens` priced at the output rate. A call that times out on our side may still have been processed and billed, so the estimate assumes it was. |
| Not started (deadline, cap or admission check failed) | the existing `end` step | 0 |

Because SDK retries are 0 (§2.2), every billable request is visible to us exactly once. If a provider's invoice ever
shows less than the recorded total, the difference is an overestimate, never an underestimate.

**Admission: atomic and concurrency-safe.** Two checks, each made under a per-supplier transaction-scoped advisory lock
(`pg_advisory_xact_lock` on a key derived from `('simulation_agent', profile_id)`). Every admission or spend decision
for one supplier therefore runs one at a time, across API threads and the worker:

1. **Before accepting a session** (`POST .../what-if`), in one transaction:
   - take the supplier's lock;
   - reject with **409** if the supplier already has a queued or running `simulation_agent` job (§2.3);
   - compute the rolling 24 h spend. If `spend + simulation_agent_session_usd > simulation_agent_daily_usd`, return
     **429** with the remaining allowance and when it frees up;
   - check the global ceiling (§2.3);
   - create the simulation and enqueue the job, then commit. The lock is released at commit.

   Because a supplier can have only one active session, and admission reserves that session's whole allowance, two
   sessions can't together exceed the daily cap.
2. **Before every model call** (in `agent.investigate`), in a short transaction:
   - take the same lock;
   - compute the 24 h spend and this session's spend so far, and a worst-case cost for the next call (estimated input
     plus `max_tokens` output, as above);
   - if either `spend_24h + worst_case > daily` or `session_spend + worst_case > session_usd`, end the session with
     outcome `budget` and the reason "spend limit", without making the call;
   - otherwise commit and make the call outside the lock. Calls aren't held under a database lock.

   The actual or estimated cost is then recorded as a step. Overshoot is bounded by the difference between one call's
   worst-case estimate and its actual cost, which is always in the conservative direction.

**What the user sees:** the what-if panel shows the remaining daily allowance, from a small read helper. A 429 says when
the allowance frees up again.

---

## 5. Data integrity

The structured results are the truth. Agent prose is supplementary.

1. **Code boundary:** only `api/simulations.py` and the worker's handler (a lazy import inside the function, as the
   other handlers do) import `tenderfit.simulation`. The existing test
   `test_mode_a_code_never_imports_the_simulation_package` is narrowed to an explicit allow-list of those two files,
   so any other Mode A import still fails it.
2. **Database:** reads go through `read_only`, and writes are limited to simulation tables plus the `job` row
   (infrastructure, not assessment data). The isolation layers and triggers from Stages 4–7 are unchanged.
3. **API-level oracle:** a new `authoritative_fingerprint` hashes the assessment, verdict, dependency, question,
   requirement, profile, confirmation, `llm_call`, `agent_step` and `verdict_cache` tables. It deliberately leaves out
   `job` and the session/login tables, which every API call or enqueue legitimately touches. Every API test asserts it
   is unchanged.
4. **Responses never mix the two:**
   - Real assessment data stays at `/lots/{id}/assessment`; simulation data stays under `/simulations/...`.
   - The assessment response gains nothing but an optional count of simulations, so a cached or reused component can't
     show a hypothetical status as real.
5. **The answer's source of truth:** `answer` returns the verified `changed` list and `recommendation` (from
   `simulation.summary`) as the primary fields. The model's prose is under `explanation` and labelled as such.
6. **No promotion path.** Nothing in the API or UI turns a hypothetical result into a real change (§11.7).

---

## 6. Failure handling

| Failure | Behaviour |
|---|---|
| Model timeout or outage during an investigation | No SDK retry. The call is recorded as an `llm_error` step with its estimated cost (§4.2), and the session ends `agent_unavailable` (nothing guessed). The job completes; the UI shows the reason and an "Ask again" action. |
| Session deadline reached (150 s) | Outcome `budget` ("time limit"). Changes already recorded remain visible. If the agent never ran the simulation, the user can **run it manually** from the same page: it's still `created` and its changes are valid. |
| Spend limit reached mid-session | Outcome `budget` ("spend limit"), with no further calls. As above, the user can still run the simulation manually. |
| Tool-call limit reached | Outcome `budget` ("tool-call limit"), as today. |
| Worker dies mid-investigation | The job's lock goes stale; with `max_attempts=1` it's closed as `failed`. A simulation stuck `running` is closed by `engine.recover_stale_simulations()`, now called from `run_once` alongside `recover_stale_assessments`. Spend already recorded still counts; an in-flight call is unrecorded, which is the one case accounting can miss. It's bounded by one call (≤ the 4096-token output cap). The UI shows "interrupted" and offers to discard it. |
| Duplicate request (double click, refresh, second tab) | The per-supplier limit in §2.3 returns 409 with the existing ids, so the client attaches to them. The frontend also disables the button while a request is in flight. |
| Queue full | 429 with `Retry-After`; nothing is created. |
| `POST /run` called twice | The guarded `created → running` update lets exactly one through; the other gets 409. |
| Run fails (DB error) | The engine already rolls back results and marks the run `failed` with `error_stage`. The API returns 500 with a generic message; detail stays in `simulation.error`. |
| Baseline superseded during a session | Allowed by design. `drift.baseline_superseded` and `baseline_on_current_version` are shown as a banner ("this compares against an older assessment"). |
| Incomplete simulations | `created` ones with no changes and older than 7 days are only hidden by the list's default filter. Nothing is deleted; discard stays explicit. |

---

## 7. Frontend workflow

**Entry point: the existing Assessment page (`/lots/:lotId`).**
- A **"What if…"** panel under the recommendation, with two ways in:
  - **Ask** (agent): a question box with examples ("What if the buyer accepted 45 days?") and the remaining daily
    allowance. Submitting returns 202, and the page shows `ProgressBanner`: "Waiting behind N tasks" (from
    `waiting_behind`), then "Investigating… up to 2½ minutes". It then navigates to the simulation. A 409 attaches to the
    running investigation; a 429 shows when to try again.
  - **Try a change** (manual, free, instant). The initial UI exposes **five options**, which cover **four** change kinds:
    1. a requirement's numeric limit (`set_condition_bound`);
    2. a fact's evidence status (`set_fact_evidence`);
    3. a fact's validity date (`set_fact_validity`);
    4. the tender deadline (`set_notice_field` on `notice.deadline`);
    5. the lot quantity (`set_notice_field` on `lot.quantity`).

    These five options use four of the nine change kinds; `set_notice_field` serves both the deadline and the quantity.
    The other five kinds (`set_condition_match`, `remove_fact`, `set_fact_value`, `add_fact`, `set_requirement_text`)
    stay fully supported by the backend and the agent, but aren't in the manual form yet.
- Each requirement row on the Assessment page gets a small "What if…" action that pre-fills the manual form.

**Simulation page (`/lots/:lotId/simulations/:simId`), always visually distinct:**
- A persistent banner: "Hypothetical. Your assessment, profile and the tender are unchanged. This is not a verdict."
  It uses a different background colour, and real status chips are never reused unstyled.
- **Header:** the question or title, status, baseline (with a link back), `as_of`, and drift and superseded warnings.
- **Hypothesis:** the ordered list of changes in plain words ("Delivery limit: 30 → 45 days"). While `created`, the user
  can add changes, then **Preview** (which requirements are reached, and why) and **Run**.
- **Result:**
  - the recommendation before and after;
  - a counts strip;
  - a **comparison table**, one row per requirement: Baseline | Without changes | With changes | Why reached. Rows with
    an `effect` are highlighted. `drift` rows are marked "differs from your assessment for other reasons".
    `needs_model` rows read "needs reading — the rules can't decide this".
- **Agent answer (if asked):** the verified changes and recommendation first, then "Explanation (written by the
  model)", then the caveats.
- **"How this was worked out":** a collapsible trace showing each step's tool, a short human summary of its input and
  output, and its cost (estimated costs are marked as such). Raw JSON sits behind a "details" toggle.
- **Actions:**
  - Discard;
  - "Try a variation", which creates a new simulation with the same changes pre-filled (a client feature only).
- **No "Make this real" link or action in phases 3–4.** The page carries only a plain sentence: "This is hypothetical.
  Changes to your profile are made on the Profile page." It isn't a link and isn't pre-filled. A promotion path is
  designed only after R6 is fixed and the path has been explicitly reviewed (§11.7).

**List:** a "Simulations" tab on the Assessment page lists this lot's simulations (status, question, effect counts),
with discarded ones hidden by default.

**Read-only demo (`VITE_STATIC=1`):** the What-if panel is shown disabled, with the existing "read-only demo" message.
Snapshot pages are not extended in this milestone.

---

## 8. Testing

| Layer | What | How |
|---|---|---|
| API | every endpoint's success path, status codes, and response shape including `hypothetical: true` | `tests/test_simulation_api.py` with `as_supplier` (the real session path), on the `world` fixture |
| Authorization | supplier B gets 404 on every A-owned endpoint (detail, changes, preview, run, results, trace, discard, what-if on A's assessment); B's list never includes A's; no session → 401; non-GET without `X-TenderFit` → 403; another supplier's fact id inside a change → 422 | parametrised over all routes, so a new route without a test fails a route-coverage assertion |
| Isolation | `authoritative_fingerprint` is unchanged across every API call, including a full what-if job | the fingerprint helper, extended |
| Spend accounting | actual costs recorded; a failed or timed-out call records an `llm_error` step with an estimated cost; the rolling 24 h window includes and excludes steps by time; settings changes take effect without a restart | the scripted model raising `LLMError` and timeouts; `created_at` set in fixtures |
| Spend admission | 429 when `spend + session allowance > daily`; a session ends `budget` before a call that would exceed either cap; **concurrency**: two simultaneous what-if requests from one supplier on two connections give exactly one 202 and one 409, and the daily cap is never exceeded | two connections and threads against the real database, as the existing concurrency tests do |
| Execution budget | `tool_turn` receives `timeout ≤ remaining − margin`, `max_retries=0` and `max_tokens=4096`; a session with less than 15 s left makes no call; a fake model that sleeps past the deadline ends the session at about 150 s, never later | a scripted model recording its arguments; a monotonic clock patched in tests |
| Shared infrastructure | **the existing worker, queue and LLM tests pass unedited.** New tests: `claim()` picks a ready assessment before an earlier agent job; `claim()` order is unchanged when no agent jobs are queued; `enqueue(max_attempts=1)` fails once with no retry; `tool_turn` without the new arguments sends today's timeout, retries and `max_tokens` | a direct queue test; `AnthropicLLM` with a stub client capturing request options |
| Queue limits | per-supplier one active job → 409; the global ceiling → 429 with `Retry-After`; `waiting_behind` counts correctly under the priority ordering; another supplier's job ids never leak through it | API tests |
| Lifecycle / failures | double run → one 409; changes after run → 409; job failure leaves a consistent state; stale recovery from the worker | API tests plus `worker.run_once` with the scripted model |
| Integration | `POST what-if` → `run_once` (scripted agent) → `GET /jobs/{id}` done → `GET /simulations/{id}` with the verified answer and trace | one test drives the real worker loop |
| Boundary | allow-list import test (§5.1) | static test |
| Frontend | the What-if panel submits, polls and shows `waiting_behind`; 409 attaches and 429 explains; the comparison table renders effect, drift and needs-model rows; the hypothetical banner is always present; only the five options (four change kinds) appear in the manual form; **no "Make this real" link exists**; static mode disables actions | `vitest` + Testing Library in `web/src/test/`, with `api` mocked, as `app.test.tsx` does |
| End-to-end | the demo stack (`docker-compose.demo.yml`, scripted model): ask, wait, see the result in a browser | a manual check through the app's browser preview, recorded in the PR. No Playwright: it would be a new dependency. |
| Live model | an opt-in smoke run through the API and the worker, checking that actual costs are recorded and the deadline holds | extend `tests/live_agent_smoke.py` (named explicitly, never collected) |

CI's zero-skip rule stays. Nothing new is opt-in except the live file, which isn't collected.

---

## 9. Implementation phases

Four PRs, in this order. Each is merged before the next starts, and each needs explicit approval before work begins.

1. **Deterministic API.**
   - Build `api/simulations.py`: list, create, detail, changes (all nine kinds), preview, run, results, discard.
   - Add `SimulationConflict`, the `lot_id` filter, and the boundary allow-list.
   - Tests: API, authorization, isolation.
   - No model and no shared-infrastructure changes.
2. **Agent as a job, with the execution and spend controls.**
   - Additive `queue.enqueue(max_attempts=)`, the non-agent-first ordering in `queue.claim()`, and
     `LLM.tool_turn(timeout=, max_retries=, max_tokens=)`. The existing worker, queue and LLM tests must pass unedited.
   - The settings (§4.2), the advisory-lock admission, per-call spend checks, `llm_error` steps and the 150 s deadline.
   - Split `agent.ask` into `create` + `investigate`; add the worker handler, `recover_stale_simulations` in
     `run_once`, the what-if and trace endpoints, the per-supplier and global queue limits, and `waiting_behind`.
   - Tests: integration through `run_once`, concurrency, and a live smoke test through the API.
3. **Frontend: viewing.** The simulation page (banner, comparison, answer, trace), the list tab, and the Ask flow with
   `ProgressBanner` and queue position. No "Make this real" link. Vitest tests.
4. **Frontend: manual changes.** The "Try a change" form with the **five** initial options (four change kinds), Preview, Run, the
   per-requirement entry points, and "Try a variation". No "Make this real" link. Vitest tests and a browser check on
   the demo stack.

---

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| A single sequential worker delays agent answers under heavy assessment load | slow answers | priority keeps assessments first by design; the UI shows `waiting_behind` honestly; a second worker replica later if needed (configuration only) |
| The priority change in `queue.claim` affects all job kinds | a regression in Mode A job ordering | the order is unchanged when no agent jobs exist; the existing queue and worker tests pass unedited; a dedicated ordering test |
| The `tool_turn` arguments touch shared LLM code | a Mode A behaviour change | keyword-only, default `None` means today's values, and only the simulation agent passes them; a test asserts the defaults are sent unchanged |
| Cost estimates for failed calls are imprecise | the cap reached slightly early | deliberately conservative; trace steps are marked `cost_estimated` |
| A worker crash during a call leaves that one call unrecorded | under-count by at most one call | bounded by `max_tokens` = 4096; stale recovery closes the run |
| A 150 s deadline is too short for large lots | more `budget` outcomes | configurable; measure on a realistic demo lot in phase 2's live smoke test before release; the user can always run the recorded changes manually |
| Real lots make the baseline payload large, so the agent is slower and costlier than the fixture suggests | cost, deadline | the per-session and daily caps; if needed, `read_baseline` returns only the conditions plus ids, with text on request (a later, measured change) |
| Users read a hypothetical as a real verdict | wrong bid decisions | separate routes and components, a persistent banner, distinct styling, no promotion path, and verified fields shown before prose |
| The model's prose contradicts the verified fields | misleading text | prose is shown second and labelled; the claim check already blocks wrong structured claims; caveats are generated by code |
| A snapshot taken at `as_of` today differs from the baseline (drift) | confusing comparisons | drift is shown separately and never counted as an effect (already in the engine) |
| `max_attempts=1` means an infrastructure blip loses an investigation | the user must re-ask | explicit "Ask again"; a failed job's partial trace and recorded spend stay visible |
| Mode A findings R5 (shadow verdict ids in confirmations) and R6 (targeted reassessment misses facts a verdict set aside) are still open | unrelated to these phases, but a promotion path would lead users into R6 | no "Make this real" in phases 3–4; fix R6 and review the promotion path separately |

---

## 11. Approved decisions

1. **Agent execution:** a background job in the existing worker, `max_attempts = 1`, no automatic retries (§2.1).
2. **Daily spend cap:** $5 per supplier over a rolling 24 h, configurable (`simulation_agent_daily_usd`), with a
   configurable $0.50 per-session admission allowance (`simulation_agent_session_usd`). Spend is the sum of
   `simulation_agent_step.cost_usd`, including conservative estimates for failed or timed-out calls. Admission is
   checked atomically, under a per-supplier advisory lock, before a session is accepted and before every model call
   (§4.2).
3. **Single-worker queue:** non-agent jobs are claimed first; one active agent job per supplier; a global ceiling of 20
   queued agent jobs; `waiting_behind` visibility. A ready assessment waits for at most one bounded agent session (§2.3).
4. **Execution budget:** a hard 150 s session deadline; each call's timeout bounded by the time left; zero SDK retries
   and `max_tokens` 4096 for simulation-agent calls. These come through additive, keyword-only `LLM.tool_turn`
   parameters whose defaults preserve Mode A (§2.2).
5. **Change types:** all nine stay in the backend and the agent; the initial manual UI exposes five options covering four kinds (`set_notice_field` handles both the deadline and the quantity), so the other five kinds stay outside the initial UI (§7).
6. **Sequence:** four PRs, in the order of §9.
7. **Promotion:** no "Make this real" link or action in phases 3–4. It ships only after R6 is fixed and the promotion
   path has been explicitly reviewed.

**Shared-infrastructure rule:** after the changes to `queue`, the worker and `llm.py`, the existing worker, queue and LLM
tests must pass **unedited**.

**Scope gate:** this document is the plan. Implementation of any phase starts only after explicit approval.

---

## 12. Phase 1 as built

**Files.**
- `tenderfit/api/simulations.py` (new): the eight routes below. It holds no simulation logic; each route makes one call into
  `tenderfit.simulation` and maps its errors. It's the only module outside the package that imports it.
- `tenderfit/api/main.py`: `include_router`.
- `tenderfit/simulation/changes.py`: `SimulationConflict(SimulationError)`.
- `store.py`, `engine.py`, `snapshot.py`: lifecycle errors now raise `SimulationConflict` (adding a change after a run,
  a second run, discarding twice or while running, a shadow or unfinished baseline). They're still `SimulationError`s, so
  the agent and existing tests are unaffected. `store.list_for_profile` gains keyword-only `lot_id` and `limit`, and its
  defaults keep the old behaviour.
- `tests/test_simulation.py`: the boundary test is now an explicit allow-list (`tenderfit/api/simulations.py` only).
- `tests/test_auth.py`: the eight routes are added to `PROTECTED`, the list whose completeness test fails when a new route
  isn't covered by the no-session check.
- `tests/test_simulation_api.py` (new) and `docs/openapi.json` (regenerated, additions only).

**Test run:** 498 passed, 0 failed, 0 skipped (`pytest -q -ra`, as CI runs it), with `ruff` and `mypy` clean. The
suite was 434 before this phase:
- 38 new API tests, plus the 8 new `PROTECTED` entries, which are parametrised (`7144ae5`);
- 18 regression tests for the review fixes M1 and M2 (`b219249`), described below.

No migration, no new dependency, and no change to the queue, worker, LLM client or Mode A code.

**Routes** (all under `/v1`; a session is required; non-GETs need `X-TenderFit`; the supplier always comes from the session):

| Route | Success | Errors |
|---|---|---|
| `GET /lots/{lot_id}/simulations?include_discarded&limit` (1–200, default 50) | 200 `{hypothetical, notice, simulations: [...]}` | 404 unknown lot; 422 bad limit |
| `POST /simulations` `{assessment_id, title?, as_of?}` (extra fields refused) | 201 detail | 404 unknown or another supplier's; 409 shadow or not completed; 422 malformed |
| `GET /simulations/{id}` | 200 detail | 404 |
| `POST /simulations/{id}/changes` `{change: {...}}`, all nine kinds | 201 `{hypothetical, simulation_id, ordinal, change, fact_id?}` (`fact_id` for `add_fact`: −1, −2, …) | 404; 409 not `created`; 422 invalid, naming the change and field |
| `GET /simulations/{id}/preview` | 200 `{hypothetical, notice, simulation_id, reached: [{requirement_id, statement, why}], not_reached}` | 404 |
| `POST /simulations/{id}/run` | 200 detail with `summary` | 404; 409 already run or discarded; 500 generic, with the run marked `failed` |
| `GET /simulations/{id}/results` | 200 `{hypothetical, notice, simulation_id, summary, results: [...]}` with the per-requirement fields of §3 | 404; 409 unless `completed` |
| `POST /simulations/{id}/discard` | 200 detail | 404; 409 running or already discarded |

**Adapted from §3, and why:**
- Lists and reads are wrapped in an object, so `hypothetical` and `notice` sit on every response.
- `error` is replaced in the detail by `failure: {stage, message}`. A `SimulationError` message is shown as written;
  anything else shows a generic message. The raw error, which may contain internals, stays in `simulation.error` and the
  server log.
- `answer` and `agent_job` are left out of the detail until phase 2.
- `results` also returns `summary`, so the comparison renders from one request.
- Statements come from the frozen snapshot, not the live requirement.
- `results` rows also carry `input_drift`, and the summary carries `execution`, `input_drift` and `recommendation.drift`
  (see "What a run uses" below).

**Verification.**
- The oracle is the existing `live_fingerprint`: every row of every non-simulation table, plus every non-simulation
  sequence. It's stricter than the planned `authoritative_fingerprint`, because session rows are created before it's
  taken. It's asserted unchanged across the full lifecycle, failed runs, CSRF refusals, cross-supplier attempts and
  concurrent runs.
- Mutation checks confirmed the tests fail when:
  - the list ignores the supplier;
  - the body accepts extra fields;
  - conflicts map to 422;
  - adding a change touches the live profile;
  - a failure message leaks internals.
- A browser check on the demo (`tenderfit_demo`, port 8001) ran the 45-day lead-time hypothesis:
  - lead time went `not_met → needs_confirmation`;
  - the recommendation went from "Blocker found" to "Relevant";
  - 10 of 11 requirements were `not_affected`;
  - a second run got 409;
  - a request without `X-TenderFit` got 403;
  - the real assessment still read "Blocker found";
  - a hash of every live demo table was unchanged.

**What a run uses: frozen versus live.** The creation-time snapshot is *not* the whole execution context.
- **Frozen at creation:** the supplier's facts, the requirements' wording and conditions, the tender fields and `as_of`.
  `snapshot_sha256` covers exactly these and nothing more.
- **Read live at run time:**
  - overrides (`confirmation`, point G6);
  - answers (`question`);
  - the live requirement rows;
  - the document pack (`document_ref`, `document_ref_state`, `document`, `passage`);
  - rejections (`requirement_rejection`);
  - the baseline record (`assessment`, `verdict`, `dependency`).

  These are listed in `simulation/inputs.py`. A test records every SQL statement a run executes and fails if it reads a
  live table that isn't tracked.
- **Consistency:** control and simulated are evaluated in one REPEATABLE READ, READ ONLY transaction, so both see the same
  state of every live input, and the effect (control → simulated) can only come from the hypothesis.
- **Drift detection:**
  - each live input is digested at creation (stored in the simulation's immutable `drift`) and again at run time, in the
    evaluation's own transaction;
  - `summary.execution` reports, per input, its tables, both digests and whether it changed;
  - `summary.input_drift.since_creation` lists the changes, per requirement where attributable;
  - `summary.input_drift.since_baseline` lists overrides and answers recorded after the baseline ran;
  - `summary.recommendation.drift` flags a recommendation that moved, even when no requirement's status did;
  - each result row carries its own `input_drift` and a note.
- **Not detected:** document or rejection changes made *before* creation are undated, so they appear only as a baseline
  vs control recommendation difference. A simulation created before this change reports that its inputs since creation
  can't be checked.

**Abandoned runs.**
- A run holds a session-level advisory lock on its own connection until its final status is committed, and Postgres
  releases it if the connection dies.
- `engine.close_abandoned_runs` marks a `running` simulation `failed` (stage `interrupted`) only when it can take that
  lock. A live run is never closed, however long it takes, and concurrent recoveries close each run once.
- The API calls it for the signed-in supplier before list, detail, run, results and discard. It writes only the
  simulation's own status.
- Limits:
  - it needs a direct database session per run; transaction-mode pooling would break it;
  - a hung but still-connected process keeps its run `running`.

**Review findings L1–L4: deferred, with reasons.** All are low severity, so none blocks this PR.

| Finding | Decision | Why |
|---|---|---|
| L1: the freeze test's hash assertion was tautological | partly addressed in `b219249`; the rest is deferred | The message no longer overclaims, `summary.execution` is checked, and the M2 tests cover live inputs. A test that a live fact edit after creation never changes control's verdict is still missing; add it with phase 2's tests. |
| L2: `as_of` accepts any date | deferred to phase 4 | The manual form will pick the date. Extreme dates only produce meaningless results for the supplier's own simulation, and write nothing live. Bound it (for example baseline ± 10 years) when the form exists. |
| L3: no cap on changes per simulation or drafts per supplier | deferred to phase 2 | Only signed-in suppliers can do this, and it costs 2.7 s for 150 changes. Caps belong with phase 2's per-supplier admission limits, so all limits live in one place. |
| L4: loose docs and contract (design doc says "adds no simulation logic"; untyped OpenAPI responses; the 500 message claims "marked failed" even before a run starts) | deferred to phase 3 | The untyped responses match the rest of the API. Typed response models are worth adding when the frontend consumes them. The wording is cosmetic. |

**Known limits.**
- Creating isn't idempotent: a double submit makes two drafts, either of which can be discarded.
- `created` drafts accumulate until discarded, and the 7-day hiding of §6 isn't implemented.
- A run is synchronous; at real lot sizes (20–80 requirements) it's still well under a second.


## 13. Phase 2 as built

Branch `feature/simulation-agent-jobs`, stacked on phase 1 (`feature/simulation-api`). The job table, queue and single
worker are unchanged in kind; nothing new runs.

**Shared infrastructure, unchanged for Mode A.**
- `queue.enqueue(..., max_attempts=None)`: `None` keeps the table default (5). What-if jobs use 1.
- `queue.claim()` orders by `(kind = ANY(LOW_PRIORITY)), run_after, id`. With no agent job queued the order is as before.
- `AnthropicLLM.tool_turn(..., *, timeout, max_retries, max_tokens)` applies them to one call through the SDK's
  `with_options`. Left out, a call keeps the client's 300 s timeout, 4 retries and 16000 tokens. The `LLM` protocol is
  unchanged. `RecordedLLM` and `DemoLLM` accept the options.
- Provider errors carry `may_have_billed`: set for timeouts, connection failures, 5xx and 408; clear for other 4xx. A
  response that arrived but was unusable (`max_tokens`, refusal) carries its real `usage`.

**Agent.** `agent.ask` is split into `begin` (create the simulation and record the question, in the request) and
`investigate` (the loop, in the worker); `ask` is still both. Before every model call, in this order:
- the session's own `Budget` (cost cap `simulation_agent_session_usd`, time cap the deadline) and the 12-call limit;
- the deadline: under 15 s left ends the session `budget`; otherwise the call's timeout is
  `min(remaining − 5 s, llm_timeout_s)`, with `max_retries=0` and `max_tokens=4096`;
- `spend.admit_call`, under the supplier's advisory lock: the rolling 24 h spend, and this session's, plus the call's
  worst case must fit $5.00 and $0.50.

A failed call is an `llm_error` step. It costs the real usage if a response arrived; the worst case (estimated input plus
`max_tokens` output, `cost_estimated: true`) if it may have been billed; nothing if the provider refused it. A model
client whose `tool_turn` doesn't accept the bounds (the test fakes) is called exactly as before.

**Worker.** A `simulation_agent` handler runs `investigate` with `default_llm()`. A simulation that's no longer waiting
(discarded, or already investigated) is skipped with no model call. `run_once` also calls `recover_stale_simulations`.
The boundary allow-list gains `tenderfit/jobs/worker.py`.

**API.**

| Route | Behaviour |
|---|---|
| `POST /assessments/{id}/what-if` `{question, as_of?}` | 202 `{simulation_id, job_id}`. 409 with the active ids if the supplier already has a question queued or running; 429 with `Retry-After` if the day's allowance can't cover a session (body: `remaining_usd`, `next_question_at`); 429 with `Retry-After: 30` if 20 agent jobs are queued; 404 for another supplier's assessment; 422 for an empty or over-long question. |
| `GET /what-if/allowance` | the daily allowance, spend, remaining, whether a question can be asked now and when, and the active question if any |
| `GET /simulations/{id}/trace` | `{hypothetical, notice, simulation_id, job: {id, status}?, outcome?, steps: [{step, tool, input, output, model, tokens, cost_usd, cost_estimated, created_at}]}` |
| `GET /jobs/{id}` | adds `waiting_behind` for agent jobs only: running jobs, ready jobs of other kinds and agent jobs ahead of it; 0 once it runs |
| changes, run | 409 while the agent's job for that simulation is queued or running. Discard is always allowed; the agent then stops. |

**Where this differs from §2–§4, and why.**
- Admission holds a **session-level** advisory lock rather than a transaction-level one, because `store.create` commits
  its own transaction. The lock is released in a `finally`, after a rollback of anything uncommitted.
- A running agent job is treated as stale after **10 minutes**, not 1 hour (`queue.AGENT_STALE_AFTER`). A session can't
  legitimately run that long, and with `max_attempts=1` it's failed, never rerun, so the supplier can ask again sooner.
  Other kinds keep 1 hour.
- The trace is an object, not a bare list, so it carries `hypothetical` like every other simulation response.
- The `agent` fields in the list and detail responses (§3) are **not** added: `trace` and `GET /jobs` give the same
  information, and the phase 1 responses stay as they are. Add them in phase 3 if the frontend needs them.
- `as_of` is accepted on the what-if body, as it is on `POST /simulations`.
- Manual changes and runs are refused while the agent works on a simulation. The plan didn't specify this. Without it,
  a person and the agent could race on one simulation's changes.

**Known limits.**
- The global ceiling of 20 is checked under the supplier's lock, not a global one, so simultaneous questions from
  different suppliers can exceed it by a few.
- The SDK's timeout bounds each network phase, not total wall time; a non-streamed response sends nothing until it's
  complete, so in practice it bounds the call.
- The deadline is checked before model calls. A tool (the rules run) that starts just before it can finish after it.
- The worst-case input estimate (characters ÷ 3) could undercount unusual text, such as non-Latin scripts.
- One worker: a ready assessment can wait for one agent session in progress (about 155 s), never for queued ones.
- Not yet done: the live smoke test through the API (it needs an API key and spends real money).

**Tests:** `tests/test_simulation_agent_jobs.py` (30 tests). The suite runs 531 tests with 0 skipped. Existing worker,
queue, LLM and agent tests are unedited. The only edits to existing tests are registrations: `test_auth.PROTECTED`, the
`routes()` list in `test_simulation_api.py`, and the boundary allow-list. Reverting each safeguard makes tests fail:

| Safeguard reverted | Tests failed |
|---|---|
| claim order ignores priority | 2 |
| `max_attempts` dropped | 4 |
| no one-per-supplier check | 2 |
| supplier lock is a no-op | 1 |
| per-call spend never refuses | 1 |
| no deadline check | 1 |
| per-call bounds never passed | 4 |
| failed call costs nothing | 1 |
| manual change allowed while the agent works | 1 |

## 14. Phase 4 as built: building, varying and comparing scenarios

Branch `feature/simulation-scenarios`, stacked on phase 3 (`feature/simulation-frontend`). It extends the simulation
model; it adds no engine, queue, worker or store. A *scenario* is a simulation. Nothing in this phase calls the model.

**Lifecycle, unchanged.** created → running → completed | failed; any of those → discarded. Changes stay append-only
while `created`, and a simulation runs once. Editing a scenario therefore happens in two places:
- before creation, in a client-side draft that nothing on the server sees until **Create**;
- after creation, by making a **variation**, which is a new simulation. The original is never altered.

**Variations (migration 021).** `simulation.parent_id` points to the scenario a variation was made from. A variation
copies its parent's frozen snapshot byte for byte, so every scenario explored from one parent is decided on identical
frozen inputs, however the live profile has moved since. The database proves the lineage:
- the composite foreign key `(parent_id, profile_id, baseline_assessment_id, snapshot_sha256)` references the same
  columns of the parent, so a variation can't belong to another supplier, another baseline or another snapshot;
- `simulation_guard()` (redefined, otherwise identical to 019's) adds `parent_id` to what can never change after
  creation.

The parent may be in any state except `discarded`. A variation carries only its own changes; the builder pre-fills
them from the parent's, so "40 days" becomes "20 days" by editing the draft. The live inputs (overrides, answers,
documents and so on) are read afresh when a variation is created, because input drift is measured from there. Only
simulation tables change. The migration touches nothing else.

**Endpoints.**

| Route | Behaviour |
|---|---|
| `POST /simulations` `{assessment_id, title?, as_of?, changes?}` | `changes` is new: the whole hypothesis, validated in order against the snapshot before anything is written. One invalid change → 422 naming it ("change 2 (remove_fact): …"); nothing is created. |
| `POST /simulations/{id}/variations` `{title?, changes?}` | 201, the new simulation, with `parent_id`. 404 for another supplier's or a missing parent; 409 for a discarded one; 422 as above. A snapshot or supplier in the body is refused (extra fields forbidden). |
| `GET /lots/{lot_id}/simulation-inputs?assessment_id=` | What a new scenario of that assessment would freeze: requirements with their conditions (`accepts` names the change kinds each condition takes), the supplier's real facts, the two notice fields, and the fact kinds with their value types. `hypothetical: false`: every value is actual. Creates nothing (READ ONLY). |
| `GET /simulations/{id}/inputs` | The same view of an existing scenario's frozen snapshot, plus its changes: what a variation starts from. |
| `GET /lots/{lot_id}/simulations/compare?ids=a,b[,c,d]` | Two to four scenarios, side by side. See below. |
| `GET /simulations/{id}` and the list | add `parent_id`; the list also gives `snapshot_sha256`. |

`/compare` sits under the lot because `/simulations/compare` would collide with `/simulations/{simulation_id}`.

**Change kinds offered by hand.** Eight of the nine (§12): every kind but `set_requirement_text`. Rules can't read
wording, so a reworded requirement could only ever come back as "needs reading"; the API still accepts that kind.
"Change an answer to a requirement" isn't offered: answers are live inputs, not part of the frozen snapshot, so a
scenario can't vary them without a new change kind. Deferred.

**Actual versus hypothetical.**
- The builder shows each target's actual, frozen value next to the input for the hypothetical one, marked **Actual**.
- Every draft change is marked **Hypothetical**.
- `add_fact` opens with a "Hypothetical evidence" warning: the fact exists only in the scenario, is never added to the
  profile, and doesn't count as evidence held. An added fact keeps its negative id (§12), so it can't be mistaken
  for a real one.
- In the comparison, changes that suppose evidence the supplier doesn't hold are flagged `assumes_evidence` ("Supposes
  evidence"): an added fact, an evidence status raised to `declared` or `document_on_file`, or a validity later than
  the frozen one.

**Limits (L3, deferred in §12, now done).** A scenario holds at most **25 changes**, and each change's JSON is at most
**4,000 characters**. Both apply to creation, variations and `POST …/changes`, in `simulation.store`, so no route can
skip them. A compare request takes 2 to 4 distinct ids; anything else is 422.

**Comparison semantics.** All scenarios must be the caller's (else 404 "simulation not found", indistinguishable from
a missing one), on this lot (404), `completed` (409, naming the others) and of one baseline assessment (409).
- **Effect** is `control → simulated` within each scenario: the same frozen snapshot without and with its changes, in
  one REPEATABLE READ. The real assessment's verdict is shown beside it, never used to judge an effect, because
  baseline → control also moves with live data (drift).
- **Direction** follows `not_met < unknown < needs_confirmation < met`. A row the rules can't decide (`needs_model`)
  is `undetermined`; an unreached row is `unchanged`. Every transition is also counted by name
  (`"not_met -> needs_confirmation": 1`), and counts stay per category: improved, worsened, unchanged, undetermined.
  Nothing is summed into a score.
- **Highlights**, per scenario, from rule-decided rows only:
  - `blockers_resolved`: mandatory, not_met → met or needs_confirmation;
  - `blockers_now_unknown`: mandatory, not_met → unknown. Kept apart because "nothing contradicts it any more" isn't
    "proved";
  - `blockers_introduced`;
  - `uncertainties_resolved`: unknown → decided;
  - `now_needs_confirmation`;
  - `needs_model`.
- **Order.** Scenarios are listed by blockers resolved (most first), then blockers introduced (fewest), uncertainties
  resolved (most), improved (most), worsened (fewest), id. The response states this rule in words; it's a sort over the
  counts shown, not a score, and it doesn't predict the buyer's evaluation.
- **Per requirement:** statement, obligation, the real verdict, the tender quote and `passage_id` (the page opens the
  source), and for each scenario control, simulated, how, direction, both reasons, the dependency reasons, notes,
  drift and input drift. Also `affected` (any scenario moves it) and `differs_between_scenarios`.

**Integrity.** `integrity` reports:
- `same_snapshot`;
- `same_live_inputs` (every live input's `at_run` digest equal across runs);
- `consistent`;
- `notes` naming each problem: scenarios frozen separately, judged as of different days, run by different engine
  versions, live inputs that changed between runs, or a scenario whose inputs changed between creation and run.

The page shows "Compared on identical inputs" only when all hold, and otherwise "Read this comparison with care" with
the notes. Comparing separately frozen scenarios is allowed but always flagged; variations avoid it.

**Prioritization (capability D), deterministic.** For every change, `reaches` lists the requirements whose recorded
dependency reasons (`impact.reach`) name the change's target:
- `requirement:{id}` for condition and text changes;
- `fact:{id}` for fact changes, and `fact:-n` for the nth added fact;
- `field:{name}` for notice fields.

Of those, `moved` lists the ones whose verdict moved, and `attributable` the ones no other change of the same scenario
reached. When several changes reach one requirement, the move is listed under each of them and attributed to none.
`priorities` groups the changes:
- `addresses_blockers`;
- `resolves_uncertainty`;
- `widest_reach`;
- `depends_on_evidence_not_held`;
- `introduces_blockers`;
- `little_or_no_effect`.

The response's `basis` says the grouping comes from dependency data and predicts nothing.

**Frontend.**
- **`/lots/:lotId/scenarios/new?assessment=`, and `?from=` for a variation.** Choose a change kind, then a target from
  only those it applies to, then a value. Check it on the client (numbers, required keys, lists), then review the draft
  list, remove items and name the scenario. **Create and run** or **Create without running**. A 422 is shown with the
  server's message and "Nothing was created".
- **Simulation page:**
  - **Run this scenario** appears for a hand-built scenario not yet run; it never appears for one the agent handles;
  - **Try a variation** appears unless the scenario is discarded or running;
  - the meta line reads "A variation of scenario N, on the same frozen copy".
- **What-ifs tab:** **Build a scenario**; a checkbox on each completed scenario of the current assessment (others are
  disabled, with the reason in the title); **Compare** for 2 to 4. Each row says what it's a variation of.
- **`/lots/:lotId/compare?ids=`:**
  - the integrity banner;
  - one card per scenario, in the stated order: its changes, recommendation without and with, the four counts, the named
    verdict moves and the highlights;
  - the requirement table: affected rows first, the rest collapsed, each with a "Tender source" button and a "Why" per
    cell;
  - "What the changes did" (the priority groups).

  Every simulated verdict uses the dashed hypothetical pill. On phones the cards stack and the table scrolls inside its
  container; the page itself never scrolls sideways.

**Where this differs from §3 and §7, and why.**
- §7 called "Try a variation" a client feature that copies the changes into a new simulation. It's server-side
  (migration 021), because a fresh snapshot would let the live profile move between scenarios, so they couldn't be
  compared on identical inputs. The prompt for this phase asked for "an explicit reference to the frozen baseline".
- The builder offers eight change kinds, not the five options in §7, because every kind but rewording is decidable by
  the rules.
- Creation with changes is one request, not a create followed by one request per change. A partly built draft can't
  be left behind by a failure, and the 422 names the change.

**Known limits.**
- Answers to requirements can't be varied (see above).
- Comparing needs completed scenarios; a scenario that failed or was never run can still be varied.
- `attributable` is conservative: with two changes that both reach a requirement, the move is attributed to neither,
  even if only one could have caused it. The page says the move is shared.
- `assumes_evidence` for a validity change compares dates only; a change to an earlier date isn't flagged.

**Tests.** `tests/test_simulation_scenarios.py` (29 tests) covers:
- atomic creation; invalid, unsupported, protected and malformed hypotheses creating nothing; the size limits;
- variations sharing the snapshot while the live profile moves; variations of variations; ownership and conflicts;
- the foreign key and the immutable `parent_id`; migration 021 touching only simulation tables;
- the inputs views and their ownership;
- comparison semantics (transitions, highlights, the not_met → unknown case, order, priorities, attribution),
  integrity for separately frozen scenarios and for live-input changes between runs, every refusal, and live-table
  immutability.

Registrations only in existing tests: `test_auth.PROTECTED` and `routes()` in `test_simulation_api.py`, both of which
fail when a route is missing.

Frontend: `web/src/test/scenarios.test.tsx` (11 tests): the builder, client checks, server refusals, hypothetical
evidence, target filtering, variations, running a draft, list selection, comparison rendering, integrity warnings,
refusals and the source dialog. The phase 3 tests run unedited.

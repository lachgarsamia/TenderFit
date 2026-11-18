# Stage 3 design: isolated simulations (Mode B foundation)

Status: **proposal, awaiting approval.** No code has been written. It builds on `docs/SIMULATION_AUDIT.md` (§ references point
there) and `docs/SIMULATION_BASELINE.md`.

## 0. The goal in one sentence

Given an existing assessment, take a frozen copy of everything it was decided on, apply validated hypothetical changes **in
memory**, re-run TenderFit's own verdict logic on that copy, and store the outcome in tables live code never reads.

```
           LIVE (Mode A, unchanged)                          SIMULATION (Mode B, new)
 ┌─────────────────────────────────────┐   read-only   ┌───────────────────────────────────────────┐
 │ assessment ─ verdict ─ verdict_*    │ ────────────▶ │ simulation           (one experiment)      │
 │ requirement ─ requirement_condition │   (FKs point  │ simulation_change    (typed hypotheses)    │
 │ profile_fact ─ notice_version       │    this way   │ simulation_result    (per requirement)     │
 │ dependency ─ question ─ verdict_cache│    only)      │                                           │
 └─────────────────────────────────────┘               └───────────────────────────────────────────┘
        ▲ never written by simulation code                  ▲ the only tables simulation code writes
```

---

## 1. How isolation is guaranteed: four layers, each checked by a test

Isolation isn't one mechanism. It's four independent layers, so any single mistake is caught by another.

| # | Layer | Mechanism | What it stops |
|---|---|---|---|
| L1 | **Separate tables** | Simulation data lives only in `simulation_*`. No live table gets a new column, flag or row. Foreign keys point from simulation → live only. | The shadow failure mode (§4 of the audit): a forgotten `NOT shadow` filter. Live queries can't see simulation rows, because those rows aren't in the tables they query. |
| L2 | **Read-only evaluation** | All computation runs inside a Postgres `READ ONLY` transaction (`conn.read_only = True`, verified to work in psycopg 3.3.7: the `INSERT` is rejected with `ReadOnlySqlTransaction`). Results are only computed in memory. | Hidden writes in reused code (`verdict_cache`, `llm_call`, `agent_step`, the cache-poisoning hazard of audit §5). Postgres *enforces* "no writes", so we don't have to trust our reading of the code. |
| L3 | **One write transaction** | After evaluation, results are inserted into `simulation_result` and the simulation is marked `completed` in a single transaction. Any exception rolls it back; then a small separate transaction marks the simulation `failed`. This is the same pattern as `pipeline._mark_failed`. | Partially written simulations, and failures that touch live state. |
| L4 | **No promotion path** | No function turns a simulation into an assessment. Mode A modules never import `tenderfit.simulation` (checked by a test). | A simulation silently becoming authoritative. |

**The isolation test oracle:** a `live_fingerprint(conn)` test helper hashes every row of every non-simulation table
(`md5(string_agg(row::text ORDER BY ...))` per table). Each isolation test takes a fingerprint before, does something, and
asserts the fingerprint is identical after. This doesn't depend on guessing *which* table might be touched: it catches a
write to **any** live table, including ones added in the future.

*Alternatives considered:*
- **A dedicated Postgres role with privileges only on `simulation_*`.** It's the strongest option, but it needs a second
  connection pool, role management in migrations, and deployment changes. L2 gives the same "database says no" guarantee
  for the evaluation phase, at no operational cost. Revisit if simulations ever run untrusted agent code.
- **A separate database or schema.** It breaks the foreign keys to `assessment` and `requirement` that make baseline
  references trustworthy.
- **`assess_lot(shadow=True)` + rollback.** Ruled out in audit §3–4: it commits mid-run, and it can't take a hypothesis
  without mutating live rows.

---

## 2. Domain model

### 2.1 The baseline snapshot (what the simulation is decided on)

A simulation freezes, at creation, everything the rule engine needs. It's built read-only from live tables:

| Part | Source | Why it's frozen |
|---|---|---|
| Baseline assessment id, lot, profile, **tender version** | `assessment` | The tender version is immutable already (trigger `forbid_update`), so a reference is enough. |
| Requirements + their conditions | `requirement`, `requirement_condition` for that version, visible to that profile | Hypotheses edit conditions; we need the original values to apply changes to and to show diffs. |
| **Supplier facts** | `pipeline.load_facts(profile, lot, version)` → `list[Fact]`, serialised | **The profile has no versions (audit §7).** The snapshot *is* the profile version: it's stored with a SHA-256 hash, so "which profile state was used" has an exact, checkable answer. It includes the per-lot confirmations `load_facts` already resolves. |
| Tender context | `pipeline.lot_context(...)` → `Context.fields` (deadline, quantity, regions, CPV, contract end) | Hypotheses can change the deadline or quantity. |
| `as_of` date | default: the day the simulation is created | Validity (expiry, staleness) depends on "today". Pinning it makes a re-run reproducible. |
| Baseline drift | facts with `updated_at > baseline.finished_at`, confirmations added since | See §2.4. |

*Decision: snapshot, not profile versioning.* Adding versioning to `profile_fact` would change Mode A's tables and every
write path in `profile/service.py`. A snapshot is local to Mode B, small (tens of facts), and exact. Live profile
versioning can come later without changing this design. The snapshot would then just record a version id as well.

### 2.2 Hypothetical changes: a closed, typed vocabulary

Each change is a Pydantic model with a literal `kind`, combined into one discriminated union, `HypotheticalChange`.
There's **no free-form patch**: a change can only express what its type allows, and an LLM can later only *propose*
these objects, which go through the same validation.

| `kind` | Fields | Validated against the snapshot | Brief's example it covers |
|---|---|---|---|
| `set_condition_bound` | `requirement_id`, `condition` (ordinal), `bound` | Condition exists on that requirement; its kind is `min_amount` / `at_least` / `at_most`; bound is finite and ≥ 0 (the same `_number` rules as the engine) | insurance £5M → £10M |
| `set_condition_match` | `requirement_id`, `condition`, `match` | Kind is `has_evidence` / `valid_through`; match is non-empty and ≤ 200 characters | required certificate changes |
| `set_fact_validity` | `fact_id`, `valid_until` | Fact is in the snapshot (and therefore belongs to this supplier) | a certificate expires |
| `set_fact_evidence` | `fact_id`, `evidence_status` | A status in the `profile_fact` CHECK set | evidence downgraded to declared / missing |
| `remove_fact` | `fact_id` | Fact is in the snapshot | remove an evidence item |
| `set_fact_value` | `fact_id`, `key`, `value` | Re-validated with **`profile.service.clean_fact()`**, the live shape rules | turnover or insurance amount changes |
| `add_fact` | `attribute`, `label`, `value`, `evidence_status`, `valid_until?` | `clean_fact()`; gets a negative id (-1, -2, …) so it can never collide with a real `profile_fact.id` | "what if we held ISO 14001?" |
| `set_notice_field` | `field` ∈ {`notice.deadline`, `lot.quantity`}, `value` | Type per field (date / finite non-negative number) | deadline or quantity change |
| `set_requirement_text` | `requirement_id`, `statement?`, `quote?` | Requirement exists | wording change. **Evaluating it needs the model, so Stage 5 reports it as "needs model"** (§3). |

Changes are applied, in order, by a **pure** function `apply_changes(snapshot, changes) -> Scenario`. It has no `conn`
and no I/O, so it can be unit tested exhaustively. Two changes to the same target are allowed; the later one wins, and
the stored order makes that auditable.

*Alternative considered:* JSON-Patch style edits on the snapshot. They're flexible, but nothing stops an edit that is
syntactically valid and makes no sense, such as setting `evidence_status` to `"maybe"` or changing a fact's `id`. A closed
vocabulary keeps simulations meaningful, explainable in the UI, and mappable to dependency targets (§4).

### 2.3 Lifecycle

```
            add_change (only here)
   ┌──────────┐ run ┌─────────┐ ok  ┌───────────┐
   │ created  │────▶│ running │────▶│ completed │
   └──────────┘     └─────────┘     └───────────┘
        │               │ error           │
        │               ▼                 │
        │          ┌─────────┐            │
        │          │ failed  │            │
        │          └─────────┘            │
        │               │                 │
        └───────────────┴──── discard ────┴──────▶ discarded (terminal)
```

- Every transition is a **guarded update** (`UPDATE … SET status = new WHERE id = … AND status = expected RETURNING id`),
  the same pattern as `assessment` in `pipeline._run`. A lost race fails loudly instead of overwriting.
- **A simulation is run once.** Its changes freeze when it leaves `created`, and its results are written once. To try a
  variation, create a new simulation; an optional `copy_changes_from` helper makes that cheap. This is the main reason
  "one simulation can't modify another": none of them is ever edited after it runs.
- **Discard is a soft state change, not a delete.** Rows are kept for audit, and nothing in live data references a
  simulation, so there's nothing to cascade into. Discarding a `running` simulation isn't allowed (the run would race);
  it can be discarded once it's `completed` or `failed`.
- Database triggers back this up: `simulation_change` rows can only be inserted while the parent is `created`, and
  `simulation_result` rows are immutable (the existing `forbid_update` trigger function is reused).

### 2.4 Comparing like with like: baseline, control, simulated

A naive comparison, "live verdict vs simulated verdict", mixes two causes: **the hypothesis**, and **drift** (facts
edited since the baseline ran, a different `as_of` date, or model verdicts we don't recompute). So each requirement gets
three statuses:

| Column | Computed how | Meaning |
|---|---|---|
| `baseline_status` | copied from the live `verdict` | what TenderFit actually said |
| `control_status` | rule engine on the snapshot, **no changes** | what the rules say now, on the frozen inputs |
| `simulated_status` | rule engine on the snapshot **+ changes** | what the rules say under the hypothesis |

- **The effect of the hypothesis is `control → simulated`.** Same inputs except the changes, so the difference is caused
  by the changes and nothing else.
- **`baseline ≠ control` is reported as drift, never as an effect.** It's useful in its own right: it shows a live
  assessment that's out of date.

*Alternative considered:* comparing against the baseline only. It's simpler, but it would credit the hypothesis with
changes it didn't cause. That's exactly the kind of misleading answer an impact-analysis tool must not give.

---

## 3. Reusing the verdict logic: the one refactor in Mode A

Audit §1 showed that `decide()` already takes facts and context as arguments, and that on the **rule-settled** path it only
*reads*. Two things stop the simulation calling it directly:

1. `_rule_outcomes()` reads conditions **from the database by requirement id**, so a changed bound would be ignored.
2. When a condition needs interpretation, it calls `_model_decision()`, which writes `llm_call` / `verdict_cache` and
   costs money.

**Proposed change (Stage 5), two keyword arguments whose defaults preserve current behaviour exactly:**

```python
def decide(conn, version_id, req, facts, ctx, llm, budget_left, emb=None, rr=None, config=..., profile_id=None,
           *, conditions: list[dict] | None = None,   # None → read from requirement_condition, as today
           interpret: bool = True) -> Decision:      # False → never call the model; mark the requirement "needs_model"
```

- With `interpret=False`, a requirement with open mandatory conditions is returned as **"needs model interpretation"**,
  with the rule-settled conditions still reported. If a rule already says `not_met`, then `parent_status` makes the parent
  `not_met` whatever the model would add, and the simulation reports that definite result.
- Mode A call sites don't change. **The whole existing suite (361 tests) must pass unmodified.**
- `run_agent` is not reachable from `decide()` (it's called from `_run`), so simulations never run the agent.
  L2 would reject its `agent_step` write anyway.

*Alternatives considered:*
- **Copy the rule path into `tenderfit/simulation/`.** No Mode A change, but two implementations of "how a verdict is
  decided" would drift apart. That's the duplication the brief forbids.
- **Pass a fake LLM that refuses.** No signature change, but it still reads `verdict_cache` by requirement id, which is
  the cache-poisoning hazard on the read side. It would also label results "model unavailable", which is false.

**Why rules-only first:** it's deterministic (same inputs, same output, so it's auditable), free, and it covers the
headline scenarios: thresholds, certificates, expiry, evidence status and deadline are all rule conditions. Model-backed
simulation needs a separate cache key that includes the requirement's text and conditions, plus a simulation-scoped trace
table. It's a later, explicit decision.

---

## 4. How this connects to dependency-aware impact (Stage 6, for orientation)

Each change type maps to the dependency targets `changes.impact.plan_reassessment` already understands:
`set_fact_*` / `remove_fact` → `("fact", id)`; `set_notice_field` → `("field", path)`; condition and text changes → that
requirement directly. Stage 6 uses `load_deps(baseline)` to recompute only the touched requirements. It's validated by a
test asserting that **selective recomputation == full recomputation** on every rule-settled requirement. That's the same
idea as the existing `shadow_full` audit, applied to simulations.

---

## 5. Persistence: migration `019_simulations.sql`

Conventions from audit §11: `bigserial` ids, `CHECK` constraints for enums, partial indexes, `forbid_update` for
immutability, no down-migration.

```sql
CREATE TABLE simulation (
    id bigserial PRIMARY KEY,
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    lot_id bigint NOT NULL REFERENCES lot (id),
    baseline_assessment_id bigint NOT NULL REFERENCES assessment (id),
    version_id bigint NOT NULL REFERENCES notice_version (id),
    status text NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'running', 'completed', 'failed', 'discarded')),
    title text,                                   -- the question being asked, in the user's words
    as_of date NOT NULL,
    snapshot jsonb NOT NULL,                       -- facts, conditions, context fields (§2.1)
    snapshot_sha256 text NOT NULL,
    drift jsonb NOT NULL DEFAULT '{}',             -- facts/confirmations changed since the baseline ran
    engine_version text NOT NULL,                  -- bumps when evaluation semantics change
    summary jsonb,                                 -- counts of changed verdicts, recommendation before/after
    error text, error_stage text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz, finished_at timestamptz, discarded_at timestamptz,
    FOREIGN KEY (baseline_assessment_id, profile_id, lot_id, version_id)
        REFERENCES assessment (id, profile_id, lot_id, version_id)   -- the baseline really is this lot/profile/version
);
CREATE INDEX simulation_profile ON simulation (profile_id, created_at DESC);
CREATE INDEX simulation_baseline ON simulation (baseline_assessment_id);

CREATE TABLE simulation_change (
    id bigserial PRIMARY KEY,
    simulation_id bigint NOT NULL REFERENCES simulation (id),
    ordinal int NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,                        -- the validated model, re-validated on read
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (simulation_id, ordinal)
);

CREATE TABLE simulation_result (
    simulation_id bigint NOT NULL REFERENCES simulation (id),
    requirement_id bigint NOT NULL REFERENCES requirement (id),
    baseline_verdict_id bigint REFERENCES verdict (id),
    baseline_status text, control_status text, simulated_status text,
    simulated jsonb NOT NULL,                      -- reason, decided_by, conditions, facts used, required/observed
    how text NOT NULL CHECK (how IN ('rule', 'needs_model', 'not_affected')),
    PRIMARY KEY (simulation_id, requirement_id)
);
-- + triggers: results immutable (forbid_update); changes insertable only while the simulation is 'created'.
```

**One change touches a live table: an additive unique index on `assessment (id, profile_id, lot_id, version_id)`.** The
composite foreign key above needs it. It can't fail, because `id` is already unique, and it changes no data or behaviour.
In return, the database guarantees that a simulation's baseline belongs to the same supplier, lot and version it
claims. The alternative is checking this only in application code.

Results are stored as self-contained JSON per requirement, **not** as copies of the `verdict_*` link tables. Hypothetical
facts have no `profile_fact` row to link to, and keeping the copies separate means no live link table ever holds simulation data.

---

## 6. Module layout: `tenderfit/simulation/`

| File | Contents | Writes to the DB? |
|---|---|---|
| `changes.py` | `HypotheticalChange` union, `validate(change, snapshot)`, `SimulationError(ValueError)` (mirrors `ConfirmationError`, `ProfileError`) | no |
| `snapshot.py` | `Snapshot` dataclass; `take_snapshot(conn, assessment_id) -> Snapshot` (read-only); `to_json` / `from_json`; `sha256` | no (reads) |
| `scenario.py` | `apply_changes(snapshot, changes) -> Scenario` (pure) | no |
| `store.py` | `create`, `add_change`, `discard`, `get`, `list_for_profile`, guarded transitions. **The only module that writes, and only to `simulation_*`.** | yes, `simulation_*` only |
| `engine.py` *(Stage 5)* | `run(conn, simulation_id)`: read-only evaluation (L2), then one write transaction (L3) | yes, via `store` |
| `compare.py` *(Stage 5)* | baseline / control / simulated deltas; recommendation via `rules.aggregate.dimensions` | no |

Every public function takes `profile_id`, and every lookup is scoped by it, the same way `profile.service.owned_fact` is.
A fact or requirement id from another supplier is reported as "not found", following the convention in `confirm.py`.

---

## 7. What Stage 4 will deliver, concretely

**Code:** `changes.py`, `snapshot.py`, `scenario.py`, `store.py`, and migration `019`. No `engine.py` yet, and no Mode A
changes at all in Stage 4. The `decide()` refactor belongs to Stage 5.

**Tests:** `tests/test_simulation.py`, using the `world` fixture pattern and a `live_fingerprint` helper. They map to the
brief's list:

| Brief | Test |
|---|---|
| 1 | creating a simulation leaves `live_fingerprint` identical |
| 2 | adding valid changes of every kind leaves `live_fingerprint` identical (live verdicts untouched) |
| 3 | simulation rows exist only in `simulation_*`; `get_assessment()` output is identical before and after |
| 4 | the simulation records the right baseline / version / profile; the composite FK rejects a mismatched baseline; the snapshot hash matches a fresh `load_facts` |
| 5 | invalid changes are rejected: unknown fact, another supplier's fact, wrong condition kind, negative or NaN bound, unknown field, change after `created` (both app and trigger) |
| 6 | discarding leaves `live_fingerprint` identical, and a discarded simulation accepts no changes or run |
| 7 | two simulations on one baseline: changes and state transitions on A leave B's rows byte-identical |
| 8 | a failure during creation (forced error after the snapshot is taken) leaves no simulation row and an unchanged fingerprint. The full failure-during-run test comes with `engine.py` in Stage 5. |

Plus pure unit tests for `apply_changes` and each change validator, which need no database.

---

## 8. Decisions I need from you (my recommendation first)

1. **Profile state:** snapshot inside the simulation (**recommended**) vs adding profile versioning to Mode A now.
2. **Additive unique index on `assessment`** for the composite baseline FK: accept (**recommended**) vs enforce in application code only.
3. **Baselines:** allow any `done`, non-shadow assessment, including superseded historical ones (**recommended**, the brief
   asks to preserve historical baselines) vs only the current one.
4. **Evaluation scope in Stage 5:** rules-only, reporting "needs model" honestly (**recommended**) vs model-backed from the start.

Out of scope, reported separately: the Mode A issue where `confirm._verdict()` accepts shadow verdict ids (audit R5).

---

## 8. Stage 5 as built

**Mode A change** (`tenderfit/assessment/pipeline.py`), behaviour-preserving; the 384 tests that existed before it pass unedited:
- `decide(..., *, conditions=None, interpret=True)`, as proposed in §3. `_uninterpreted()` returns either the definite
  `not_met` a mandatory rule already decides, or `decided_by="needs_model"` (returned before the gap and conflict checks,
  which only refine a decision that exists).
- `_rule_outcomes(..., conditions=None)` evaluates the given condition rows instead of reading them.
- `lot_dimensions()` is extracted from `_run` unchanged, so the simulation computes its recommendation with the same code
  instead of a copy.

**`tenderfit/simulation/engine.py`**: `run(conn, profile_id, simulation_id)`:
1. a guarded `created → running` update, committed (a simulation runs once; a second call fails);
2. inside `read_only`: verify the snapshot hash, build control (no changes) and simulated scenarios, call the live
   `decide(interpret=False)` for each requirement on each, and compute both recommendations;
3. one transaction: insert `simulation_result` rows and set `completed` with the summary.
A failure in 2 or 3 rolls back and marks the run `failed` with `error_stage` (`evaluation` / `recording`), then re-raises.
`recover_stale_simulations()` closes runs whose process died. Since §13 it uses the run lock, so it never closes a live run.

The model is replaced by `NO_MODEL`, which raises if called. A requirement whose wording changed, or that only the model
can decide, is recorded `how = "needs_model"` with a NULL status. For the recommendation it keeps its baseline verdict in
both control and simulated, and the summary says `complete: false`.

**`tenderfit/simulation/compare.py`** (pure): effect = control → simulated; drift = baseline → control. `touched` is
computed from the facts and fields the decisions actually used. Stage 6 replaces this with the recorded dependencies.

**What a run still reads live:** the requirement row's references and lineage, answered questions, overrides, the document
inventory and rejected requirements. These are read at run time, not snapshotted, and can differ from what the baseline
saw. Since §13 they are tracked, reported as input drift when they change, and read in one REPEATABLE READ transaction,
so control and simulated see exactly the same values.

---

## 9. Stage 6 as built: dependency-aware impact

**What the audit of the dependency records found.** A verdict records the facts it *used* (`verdict_fact` → `dependency`
kind `fact`), the fields its rules resolved, and the passages it read. That's necessary but not sufficient for "what can
change this verdict":
- a rule condition on `capacity.lead_days` selects among **every** capacity fact (`rules.engine._select`), so a fact it
  set aside, or one that doesn't exist yet, leaves no record;
- every selected fact's validity is measured at the **deadline** (`profile.validity.check`), but the deadline is recorded
  only when the fact that was used has an expiry date.

On the fixture, the BS EN 1729-2 verdict recorded only test report 10 (evidence `missing`). Adding a new test report
changes that verdict, and no recorded dependency points to it.

**Design (`tenderfit/simulation/impact.py`, pure).** Each requirement's reach has two sources, and both are reported:
- `used`: the baseline's recorded dependency (`changes.impact.load_deps`). This is the explanation in the supplier's terms.
- `examines`: derived from the conditions themselves. For a rule condition, that's the fact attribute, `notice.deadline`,
  any `lot.`/`notice.` field named in `bound`/`anchor`/`min_units`, and the field defaults for `region_covered` /
  `within_years`. For a requirement only the model can read, it's its category's facts (`pipeline.CATEGORY_FACTS`, or all
  of them for `other`) and the deadline.
- `direct`: the hypothesis changes the requirement's own conditions or wording.

The union is a superset of what `decide()` reads on the rule path, so it's sound by construction for these change kinds.

**Engine.** `run(selective=True)` computes control for every requirement, and simulated only for those reached. An
unreached requirement is `how = "not_affected"` and its simulated verdict is its control verdict. `run(selective=False)`
recomputes everything and **fails the run** if an unreached requirement came out differently: it's a verification mode,
not just a slower one. `preview()` reports the reach before a run, inside a read-only transaction.

Two consequences:
- A requirement only the model can decide, and that no change reaches, keeps its baseline verdict *legitimately*, so the
  recommendation is `complete` unless a change reaches such a requirement.
- Live edits since the baseline are put through the same `reach()`. A model-only verdict they reach is flagged "baseline
  may be out of date" (`summary.stale_baseline`).

**Tests (`tests/test_simulation_impact.py`).**
- Selective == full across 17 hypotheses: every change kind singly, the fixture's edge cases, and all of them together.
  Each full run is also the self-check above.
- A test that replaces the analysis with recorded-dependencies-only shows the full run refusing to complete.

**Mode A finding, reported separately (R6), not fixed.** The live targeted reassessment has the gap described above.
`api/assessments.put_fact` re-queues only the lots whose verdicts *used* the edited fact (`profile.service.verdicts_using`),
and `changes.apply.apply_profile_change` reassesses only requirements with a recorded `fact` dependency on it. Editing a
fact a verdict set aside (for example, giving the missing BS EN 1729-2 report a different product, or a second insurance
policy) leaves the live verdict stale. I found this by reading the code; no test demonstrates it yet. The `examines`
closure in `simulation/impact.reads()` is a candidate fix, but that's a Mode A change for its own decision.

---

## 10. Stage 7 as built: the what-if agent

**`tenderfit/simulation/agent.py`**: `ask(conn, profile_id, baseline_assessment_id, question, llm, created_by)` creates one
simulation (titled with the question), then runs a single tool-using loop.
- **Tools:** `read_baseline`, `read_facts` (the snapshot, not the live profile), `add_change`, `preview`,
  `run_simulation`, and two ways to finish: `finish` and `cannot_simulate`.
- **What decides:** the model only proposes. Every change goes through `store.add_change` validation, and every verdict
  comes from `engine.run`.
- **The answer is checked against the results:** `finish` is accepted only when its `changed` list equals the stored
  results exactly. Otherwise the model is shown the real changes and has to try again.
- **Caveats are generated, not written by the model:** needs-model requirements, drift and stale baselines.
- **Every answer is labelled:** "A hypothetical result … not a verdict."

**Why not reuse `agent.loop.run_agent`:** it writes `agent_step` and `llm_call` (live tables) on every step, and its tools
read the live profile and the documents. The new loop reuses `agent.loop.Budget`, the `LLM` tool-turn protocol and the
`untrusted()` wrapping. The trace and costs go to **`simulation_agent_step`** (migration `020_simulation_agent.sql`),
which is append-only and closed once the simulation is discarded. One step is committed at a time, so the trace survives
a failure later in the session.

**Limits:** at most 12 tool calls and the lot cost, token and time caps (`Budget`). An unreachable model ends the session
as `agent_unavailable`, and nothing is guessed.

**Tests (`tests/test_simulation_agent.py`, 17, with a scripted model):**
- isolation: the whole `ask` leaves `live_fingerprint` unchanged. A mutation that logs to `llm_call` makes this test fail;
- the answer check: wrong claims are refused, and so is an answer given before the run;
- model mistakes: invalid changes are reported back and can be corrected; another supplier's facts are unreachable;
  unknown tools (`update_fact`, `record_verdict`) do nothing;
- the id of an added fact is returned so later changes can use it;
- ending without an answer: unsupported questions, the tool-call and cost limits, an unreachable model, text-only turns;
- the trace: append-only, and closed once the simulation is discarded;
- per-supplier scoping of the answer and the trace.

**Not done in Stage 7:**
- No API route or UI yet. Running an LLM session inside a request, rather than as a job, is a decision to make
  separately.
- No live-model evaluation yet: every agent test uses a scripted model.
- The answer prose isn't checked; only the structured claims are. A model could still write something misleading in the
  free text, so a UI should show the verified `changed` and `recommendation` fields, not just the prose.

---

## 11. Validation before merge

**Real-model smoke test** (`tests/live_agent_smoke.py`). It isn't collected by default, because CI fails on any skipped
test. Run it by naming it: `LIVE_LLM=1 bash scripts/test.sh tests/live_agent_smoke.py -s`.
The baseline is built with the scripted model; only the agent uses `claude-opus-5-5`.

| Question | Outcome | Verified change |
|---|---|---|
| What if the buyer accepted our 45-day lead time? | answered | lead time `not_met → needs_confirmation`; blocker cleared |
| What if our BS EN 1729-2 test report were on file? | answered (chose fact 10 itself) | standard `unknown → met`; lead time still blocks; installation flagged `needs_model` |
| What if the lot quantity were 400 desks? | answered | quantity `met → not_met` (capacity 300) |
| What if we cut our price by 10%? | `cannot_simulate` | none: price is not assessed |

No claims were rejected, no invalid changes were proposed, and the live fingerprint was unchanged. The four questions cost
about $0.22 in total.

**Found by the live run, and fixed:**
1. *The API rejected the `add_change` tool.* Strict tool schemas need every object's properties declared, and `change`
   was an open object. It's now a JSON string, still validated strictly by `store.add_change`. A new unit test applies
   the strict-schema rules to every tool. The failure was safe (`agent_unavailable`, nothing guessed, nothing written).
2. *The model mislabelled an unaffected model-only requirement as "needs model"*, because it was shown blank statuses.
   `run_simulation` now says what each `how` means, including "its baseline verdict (met) stands".
3. *A generated caveat read "requirements 3 need".* Fixed, and covered by a test.

**Found by self-review, and fixed:** `python -m tenderfit.demo reset` failed with a foreign-key violation once a demo
lot had simulations. It now deletes that lot's simulations first; their changes, results and trace cascade with them.
A regression test reproduced the failure before the fix.

---

## 12. The API (Mode B, phase 1)

Simulations are exposed over HTTP by `tenderfit/api/simulations.py`. It covers listing a lot's simulations, creating
from a completed assessment, detail, adding a change (all nine kinds), preview, run, results and discard. It's
synchronous and rules-only, and it has no agent route yet. The module adds no simulation logic: each route makes one call
into this package, with the supplier taken from the session. Errors map to:
- 404 for `SimulationNotFound`, so another supplier's simulation looks the same as a missing one;
- 409 for `SimulationConflict`, the lifecycle errors;
- 422 for other `SimulationError`s.

It's the only module outside the package that imports it, and the boundary test now names it as the single allowed
entry point. Nothing promotes a simulation into the live assessment. `docs/SIMULATION_API_PLAN.md` §12 has the routes
and how they were verified.

---

## 13. Review fixes: abandoned runs (M1) and live-input drift (M2)

### M1: a run whose process died could never be discarded

**Root cause.**
- `engine.run` marks a simulation `running`, evaluates, then commits `completed` or `failed`. If the process dies in
  between, nothing marks it, and `store.discard` refuses a `running` simulation.
- The only recovery function, `recover_stale_simulations`, had no caller, and it judged liveness by age alone, so a slow
  live run older than its threshold would have been closed.

**Fix.**
- `engine.run` takes a session-level advisory lock, `pg_try_advisory_lock(RUN_LOCK, id)`, on its own connection before
  the `created → running` update. It holds the lock until after the final status is committed, and releases it on every
  path. A connection that dies releases it too, because Postgres drops the session's locks.
- `engine.close_abandoned_runs(conn, profile_id?, simulation_id?, older_than_minutes?)` marks a `running` simulation
  `failed` (stage `interrupted`) only if it can take that lock in its own transaction:
  - a held lock means a live run, which is never closed, however old;
  - the transaction-level lock also makes concurrent recoveries close each run exactly once.
- `recover_stale_simulations` now delegates to it with an age filter, which makes it safe for a periodic sweep.
- The API runs `close_abandoned_runs` for the acting supplier before list, detail, run, results and discard. It writes
  only the simulation's own status, so even a GET touches nothing live. Abandoned runs are found without a new worker,
  service or table.

**Limits.**
- It relies on a direct Postgres session per run. A transaction-pooling proxy (for example PgBouncer in transaction
  mode) would break session-level advisory locks; none is used today.
- A process that hangs without dying keeps its lock, so its run stays `running`. That's correct, since it can't be told
  apart from a slow run.
- Lock keys are the simulation id modulo 2³¹−1. A collision can only make recovery wait, or a run of the other
  simulation get a 409.

### M2: the creation-time snapshot was not the whole execution context

**What is frozen and what is read live.** The snapshot (and `snapshot_sha256`, whose meaning is unchanged) covers only
supplier facts, requirement wording and conditions, tender fields and `as_of`. A run also reads these live tables,
listed in `simulation/inputs.py` (`TABLES`):

| Input | Tables | Scope |
|---|---|---|
| overrides | `confirmation` (point G6) | per requirement, via its lineage |
| answers | `question` | per requirement, via its lineage |
| requirements | `requirement` | per requirement |
| documents | `document_ref`, `document_ref_state`, `document`, `passage` | lot |
| rejections | `requirement_rejection` | lot |
| baseline_record | `assessment`, `verdict`, `dependency` | lot; it should never change, so it is checked rather than assumed |

A test records every SQL statement a run executes. It fails if a run reads a live table that isn't in this list, so a
new live read can't go unreported.

**The fix.**
1. *Same inputs for control and simulated.* `isolation.read_only` now opens a REPEATABLE READ, READ ONLY transaction,
   so every statement of the evaluation sees one database state. Before, under READ COMMITTED, an answer committed
   between the control and simulated decisions of one requirement could produce a false effect. A test commits one at
   exactly that moment.
2. *Provenance.*
   - At creation, `inputs.read` stores a digest of each input (per requirement where attributable) in the simulation's
     `drift`, as `live_inputs_at_creation`; migration 019's guard makes `drift` impossible to rewrite. The API leaves
     these digests out of the detail's `drift`.
   - At run time, the same digests are read in the evaluation's own transaction, so they describe exactly what was used.
   - `summary.execution` reports the snapshot hash, what it freezes, and for each live input its tables, its digest at
     creation and at run time, and whether it changed.
3. *Drift that moves no status.*
   - `summary.input_drift.since_creation` lists inputs that changed between creation and run.
   - `summary.input_drift.since_baseline` lists overrides and answers recorded after the baseline ran.
   - `summary.recommendation.drift` is true when the control recommendation differs from the baseline's, even if no
     requirement's status moved. For example, an override changes only how a not-met verdict is aggregated.
   - Each result row carries `input_drift` and a note, so a requirement whose status stayed `not_met` still says that an
     override was recorded.
   - A simulation created before this change reports `input: "all"`: changes since creation can't be detected for it.

**Guarantees, stated exactly.**
- The snapshot hash proves the frozen part.
- `summary.execution` proves which live state the run used.
- Input drift reports changes to the tracked inputs, since creation (by digest) and since the baseline (overrides and
  answers, by timestamp).
- Changes to documents or rejections before creation are not dated, so they show up only in the baseline-vs-control
  recommendation.
- The effect (control → simulated) is unaffected by any of this: both sides always read the same inputs.


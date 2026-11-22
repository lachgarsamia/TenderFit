# Phase 5B: frozen answers and hypothetical overrides

Implemented on `codex/scenario-answers`, stacked on Phase 5C (`codex/scenario-attribution`).

New scenarios use `sim-v3`. At creation, the snapshot freezes each requirement lineage's ordered supplier answers and whether it has a G6 override. Both are covered by the snapshot hash. Later live answers or overrides do not affect these scenarios. Variations inherit the exact snapshot and engine version.

Two new change kinds are available through the API, manual builder and what-if agent:

- `set_requirement_answer {requirement_id, answer}` replaces all frozen answers for that requirement with one declaration. `null` removes them. Text must contain 1–2000 characters after trimming, with a maximum input length of 2000.
- `set_override {requirement_id, on}` changes the frozen override flag; `on` must be a boolean.

A declaration can move a rule's `unknown` result to `needs_confirmation`; it cannot prove `met` or clear `not_met`. Requirements that need model interpretation still report `needs_model`. Overrides affect recommendation aggregation only. They do not change requirement verdicts or the actual assessment. Result and comparison views separately display hypothetical override changes, including those with no verdict movement.

`pipeline.decide(answers=None)` keeps Mode A's existing live reads. An explicit list, including `[]`, replaces those reads. Both the rule and model paths honor that distinction. Simulation evaluation remains read-only and never invokes the verdict model.

## Compatibility and migration

Migration 022 extends the simulation change-kind constraint, retaining all nine previous kinds. No live assessment tables change. Applying the migration again is safe.

`sim-v1` and `sim-v2` JSON round-trips preserve their exact hashes and continue reading tracked live answers and overrides. New answer/override edits are rejected for these snapshots; create a new scenario from the assessment to use them. Their variations keep the old semantics. The builder hides unsupported controls.

Answers and overrides recorded between the baseline assessment and scenario creation are reported as frozen drift since the baseline. For v3 they are no longer listed as live inputs. Requirements, documents, rejections and the baseline record remain tracked live inputs.

## Validation

Regression coverage exercises declaration-only behavior, replacement/removal, immutable frozen inputs despite live edits, supplier isolation, variations, legacy hashes and semantics, strict input validation, no live decision-table reads during v3 evaluation, unchanged live-table fingerprints and aggregation-only overrides. Browser tests cover replacement, removal, override creation and legacy controls. The full backend suite protects Mode A; migration tests apply all migrations twice.

No real-model evaluation was performed for the expanded agent vocabulary. Human calibration labels remain pending under `eval/validation/`. The next roadmap milestone is Phase 6's deterministic analysis tools.

# Phase 5C: bounded multi-change attribution

Built on the provenance milestone (PR #11), on `codex/scenario-attribution`. This change adds deterministic alternative checks to newly run scenarios. It does not change the primary verdicts, snapshot schema or engine version, and old completed results remain readable without invented attribution.

## What is evaluated

For every requirement whose rule verdict moves and which is reached by at least two changes, evaluate:

- each reaching change alone;
- the complete hypothesis with that change removed.

These calls use the same `_decide` function, `interpret=False`, frozen snapshot and READ ONLY / REPEATABLE READ transaction as the main control/simulated pair. No model calls or live writes are added. Each successful alternative records its verdict, reason and evidence provenance, including original tender citations and actual versus assumed facts.

The entire simulation has a cap of **200 additional decide calls**. Two slots must remain before starting a pair. Requirements are visited by ID, then changes by original ordinal. Entries that do not fit explicitly say `limit`; no partial pair is classified. Invalid subsets consume no decide calls, but may be refused when insufficient budget remains to start their pair. A model-only alternative is `needs_model`, never guessed.

When only one change reaches a moved requirement, the existing dependency attribution is retained and no extra evaluation is claimed. Unchanged and model-only primary results get no counterfactual analysis.

## Exact meaning of labels

Labels are overlapping properties of the two checked alternatives. Let C be the control verdict, S the full scenario verdict, A the change-alone verdict and W the verdict without that change:

| Label | Test | Meaning |
|---|---|---|
| `sufficient_alone` | A = S | This change alone reproduces the full scenario's verdict |
| `necessary` | W ≠ S | Removing this change changes that verdict |
| `jointly_required` | A ≠ S and W ≠ S | This change requires other changes to reproduce S in the checked alternatives |
| `redundant` | W = S | The other changes reproduce S without this one |
| `no_independent_effect` | A = C and W = S | It moves neither the control verdict alone nor the full verdict when omitted |

For example, two alternative remedies can each be both `sufficient_alone` and `redundant`. The UI reports both facts, without choosing a single cause. “Necessary” means necessary to reproduce this exact verdict in this hypothesis, not necessary for any possible improvement. “Jointly required” does not establish a globally minimal set. These definitions refine the roadmap's ambiguous single-class taxonomy; no causality, score or probability is claimed.

## Dependent and ordered changes

Subsets preserve the original order, original change ordinals and original negative IDs for added facts. If original change 2 adds fact -2, it remains -2 even when change 1 is omitted. An edit to an omitted fact is invalid; it is never silently redirected to another fact, supplemented with missing prerequisites or relabelled as having no effect.

A pair with either an invalid subset or a model-only verdict is `undetermined`, with the available probe details and no classification. The property test checks order invariance for independent, commuting edits; repeated edits to the same value intentionally retain their specified order.

## API and presentation

- The existing result JSON gains `attribution`; the summary records the call count, cap and whether the cap was hit.
- Comparison cells carry the same recorded analysis. `reach_by_change` adds `counterfactuals` per change; legacy `attributable` and `shared_with_other_changes` retain their conservative dependency semantics.
- Individual results and comparison cells expose **Which changes matter?** with the checked alternatives, reasons, evidence and explicit limitations. No additional API call or route is introduced.
- Existing completed results lacking the field display an unavailable message. Run a variation to obtain the new analysis; old rows are never rewritten.

## Validation and remaining work

Tests cover the overlapping labels, commuting permutations, ordered edits, original negative IDs, missing prerequisites, shared evaluation budget, model-only alternatives, legacy JSON, source provenance, actual transaction settings, determinism, live-table fingerprints and supplier isolation. Browser component tests cover the new disclosures and comparison integration.

Phase 5B answer variation remains next: freeze answers under a new `sim-v3` snapshot while retaining the live-answer behavior of `sim-v1` and `sim-v2`. Phase 6 analyses and the Phase 7 evaluation gate remain outstanding. This phase does not claim real-model accuracy or user-study validation.


Validation: **584 backend tests passed, 0 skipped**, using `VERSIONRAG_ENABLED=false DEMO_SCRIPTED_LLM=false REGISTRATION_ENABLED=false` and the disposable test database. **60 frontend tests passed**; TypeScript, production build, Ruff, mypy and the reviewable-file Gitleaks scan passed. OpenAPI was regenerated and is unchanged because no route or request schema changed. No paid model run, browser visual audit or moderated user study was performed.

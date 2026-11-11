# Research roadmap

What exists now is the means to run these experiments. **No result below exists yet, except where one is stated, and that one is on synthetic data.**

## The first requirement: a labelled dataset

Every question needs real notices with independent labels. The harness is ready (`tenderfit/eval/run.py`), including agreement between annotators. It needs:

- 20 to 30 frozen real notices across sectors, weighted toward those with real amendments.
- Two annotators, ideally one with procurement experience.
- Labels for requirements, conditions, verdicts per fictional supplier, and evidence passages. The retrieval and citation-entailment metrics report "not measured" until these labels exist.
- A test set frozen before any tuning.

## Questions

### RQ1. Can LLM decisions be updated incrementally with a measured miss rate?
*The most original question here, in my judgement. Check the literature before claiming novelty.*

Treat reassessment as incremental computation: record what each decision consulted, and reuse it unless something consulted changes. Related ideas: build systems, self-adjusting computation, regression test selection.

- **Have:** per-verdict dependencies, static and dynamic plans, full reassessment as a reference, a background audit on pursued lots, a mutation suite.
- **Synthetic result so far:** see the validation report. Eight mutations, scripted model. Static dependencies missed 1 of 8 changed verdicts, and dynamic missed 0, at more reassessment. This shows the bookkeeping behaves; it says nothing about real models.
- **Next:** real amendments and a real model. Repeat full runs to separate model noise from real misses, since a model is not deterministic.
- **Measures:** missed-change rate with a clustered interval, reassessment precision, cost saved.

### RQ2. Does following explicit references beat similarity search for cross-document requirements?
Arms B1 against B3 on the `hard` subset. Pre-registered rule in `decisions()`: keep only if macro-F1 on hard cases rises by at least 0.05 and false MET does not rise.
A negative result is useful: it would argue against graph retrieval for this task.

### RQ3. Does a constrained agent earn its cost over graph retrieval?
B3 against B4 against B5. Cost per lot must include extraction, and the harness now counts it.

### RQ4. Can a system know when not to decide?
Evidence quality labels exist as rules. Are they informative? Plot accuracy against coverage per label. Only with enough labelled verdicts would a calibrated score be justified; none is shown today.

### RQ5. Which validation catches which extraction errors?
Measure each check's precision and recall on real model output: digit match, statement numbers, condition traceability, lot validation. Which false verdicts do they prevent, and which correct requirements do they wrongly drop?

### RQ6. Does automatic evidence checking help a person?
A small user study: time and errors when reviewing evidence, with and without the checks. It also needs real certificates, which raises its own data questions.

## Experiment arms

| Arm | Rules and guard | Corrective | Graph | Agent |
|---|---|---|---|---|
| B0 | No | No | No | No |
| B1 | Yes | No | No | No |
| B2 | Yes | Yes | No | No |
| B3 | Yes | No | Yes | No |
| B4 | Yes | No | No | Yes |
| B5 | Yes | Yes | Yes | Yes |
| B5-full | As B5, full reassessment on amendment | | | |

**B0 cannot be the exact earlier system.** It turns off rules and the guard in today's code. Say so if you compare with it.

Each run records the git commit, model, prompt versions, configuration, dataset version and date. Results are bootstrapped by notice, not by requirement, because requirements within a notice are not independent.

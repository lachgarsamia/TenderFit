# Evaluation datasets

One directory per dataset with `gold.json`, release snapshots (`releases/*.json`, raw OCDS releases as the API
returned them) and document files (`docs/`). Open notices are frozen as snapshots so runs are reproducible.

`gold.json`:

- `today`: the date the assessment is run as (validity and deadlines are measured from it).
- `notices[]`:
  - `releases`: the first release to import; `amendments[]` list later releases with `affected` (gold requirement
    ids whose correct verdict or inputs change) and `type` (direct, or the indirect kind: definition, appendix,
    deadline, quantity, clarification, split lot).
  - `documents`: notice document URL → local file served in place of the live link. Links not listed behave like
    a buyer portal (not retrievable).
  - `uploads`: files the user uploads for a listed document (`ref_label`); `confirm_none`: documents the buyer did
    not issue (G7).
  - `essential_documents`: labels an annotator judged essential (for coverage recall).
  - `lots.<no>.profiles.<name>.relevant`: relevance label per profile.
  - `requirements[]`: `id`, `lot`, `statement`, `quote_contains` (a phrase from the source text, used to match the
    extracted requirement), `obligation`, `hard` (evidence needs a cross-reference, clarification, a table across
    pages or a second lookup), `verdicts.<profile>` and `verdicts_after.<profile>` (after the amendment).

Two annotators label independently; agreement is reported with
`python -m tenderfit.eval kappa a/gold.json b/gold.json`.

Run: `python -m tenderfit.eval run eval/datasets/<name> --configs B1,B2,B3,B4,B4-full [--live]`.
Without `--live` only recorded model responses are used (`eval/recordings/`).

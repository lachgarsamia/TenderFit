# Validation programme intake (Phase 9 start)

Status: preparation only. No new human labels, accuracy results or user-study results exist.

`candidates.json` groups the existing 48 frozen notice projections into 24 procurements. It preserves each source hash and URL and leaves annotators, adjudication and supplier profiles unassigned. These are **calibration candidates**, because their pilot labels and sources have already been inspected. They cannot become a genuinely unseen test set by renaming their partition.

Before measurement:

1. Recruit two independent annotators, including one with procurement experience, and an adjudicator for disagreements. Use fictional profiles or document consent for real profiles.
2. Acquire complete document packs, recording retrieval date, licence, original bytes, SHA-256 and unavailable documents. Notice-only cases remain explicitly limited to notice fields.
3. Select additional, previously unused procurements across sectors. Keep every version and lot of a procurement in the same partition. Freeze the test manifest and hashes before tuning, and keep its labels out of development prompts and fixtures.
4. Have annotators independently identify each requirement, obligation, atomic condition, source quote and page; then label verdicts against the frozen profile, supporting or contrary evidence, uncertainties and amendment effects. Missing evidence is not a negative capability label.
5. Retain both annotations and disagreements. Adjudicate with a written rationale; do not silently replace disagreements with a model-generated answer. Mark incomplete or ambiguous cases separately.
6. Evaluate extraction, retrieval, verdicts (including false `met`), citations and amendment misses separately. Report denominators, exclusions and uncertainty intervals by procurement. Measure repeatability separately from accuracy.

Each annotation record should contain: `procurement_id`, `notice_id`, `lot`, `annotator_id`, `annotated_at`, `supplier_profile_sha256`, `pack_sha256`, `requirement_id`, `statement`, `obligation`, `conditions`, `source` (file hash, page, exact quote), `verdict`, `supporting_evidence`, `contrary_evidence`, `uncertainties`, `amendment_effect`, and `rationale`. An adjudicated record also names the two input annotations and adjudicator.

Human recruitment, pack collection, a sealed holdout, annotation, adjudication and the final evaluation report remain outstanding. This intake file makes no accuracy claim and does not satisfy the Phase 7 evaluation gate.

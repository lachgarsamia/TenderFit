# Supplier and amendment baseline, 11 November 2026

Reviewed release-hardening at be56872. Hold the merge pending review of historical replay and evidence provenance. The application code was unchanged during this review.

## Executed checks

Python 3.12, PostgreSQL 16.15, pgvector 0.8.7, disposable local database. The complete backend suite passed with 349 tests and zero skips. All 14 frontend tests, TypeScript checks and the production build passed. The walkthrough used FastAPI sessions and the application services, a fixture procurement feed, scripted model responses, a hashing embedder and an overlap reranker. Browser interaction, the live Find a Tender feed and live model quality were outside this run.

| Step | Observed result |
|---|---|
| Register and sign in | Enabled registration on the disposable instance. Created two accounts, signed each out and back in, and created a separate supplier profile for each. The suite also verified the default closed-registration setting. |
| Enter evidence | Added a £10m public liability fact for the first supplier, uploaded a PDF with a supporting quote, and received a verified support link. |
| Check access | The second account saw an empty fact list. Attempts to select the first profile or verify its fact returned 404. The isolation suite also exercised private files and passages, retrieval and prompts, assessment history and trace, jobs, amendments and supplier ID spoofing. Migration 014 leaves legacy tender uploads with unknown owners public. |
| Import and assess | Imported the two-lot fictional Harrowgate notice through the importer, processed its pack, extracted requirements and assessed the seeded suppliers and the newly created supplier. The new supplier saw nine Lot 1 requirements. Its policy met the financial threshold, but its empty product profile yielded low relevance. The seeded Brightwell profile supplied a useful delivery and expiry comparison. |
| Verify database setup | The migration tests applied all 18 migrations to an empty database, repeated them with zero pending, upgraded populated older schemas, checked constraints and indexes, verified the vector extension and exercised failure rollback. The API and reassessment used that PostgreSQL setup. |

## Amendment trace

Brightwell Lot 1 had a 45-day declared lead time. Version 1 required delivery within 30 days. Version 2 changed section 3.2 on page 3 to allow 45 days and moved the submission deadline from 2 to 16 December 2026. Brightwell's public liability policy expired on 9 December.

| Requirement | V1 | V2 | Explanation |
|---|---|---|---|
| Deliver desks | Not met | Needs confirmation | Declared lead time now fits. Available workload still needs confirmation. |
| Public liability of at least £5m | Met | Needs confirmation | Cover of £10m meets the threshold. The later deadline passes the policy expiry. |
| Supply 200 desks | Carried forward on amendment | Same verdict | The quantity requirement and supporting capacity fact were unaffected. |

For Brightwell Lot 1, five requirements were reopened and six verdicts retained. The other three reopened requirements were employers’ liability, ISO 14001 and comparable experience. Their statuses held. The four supplier/lot impacts produced by the walkthrough agreed with fresh full assessments on every requirement ID and status, checked in both directions. The suite also exercised a failed first amendment and a successful retry.

## Findings

1. **Historical assessment uses current notice fields.** Reassessing Brightwell Lot 1 with an explicit v1 ID after v2 was imported changed the original public liability verdict from Met to Needs confirmation. The policy and assessment date were unchanged. The old stored result remained intact. In [pipeline.py](../tenderfit/assessment/pipeline.py), lot_context reads the current version and mutable lot fields before assess_lot applies the requested version ID. A delayed shadow audit can take this path.
2. **Carried-over retrieval metadata can identify the wrong document.** The walkthrough produced 34 retained retrieval entries whose filename was Specification_v1.pdf while the linked passage belonged to Specification_v2.pdf. The spare-parts requirement was one example. The carry routine remaps the passage ID and copies the old document, section and page metadata. Only the filename mismatch was observed here.
3. **The regression comparison drops the lot key.** The three seeded supplier/lot assessments produced 22 v1 verdict keys. The normalization in [regression.py](../tenderfit/eval/regression.py) reduced them to 17 supplier/statement keys before comparing statuses. A disagreement in one lot can be overwritten by the same statement in another lot. In addition, shadow_full in [apply.py](../tenderfit/changes/apply.py) compares only mandatory IDs present on both sides. Missing or additional mandatory requirements escape its agreement check. The independent two-way comparison above found no such discrepancy in this walkthrough.
4. **Previously documented amendment visibility race.** If a recheck advances the last current assessment before the change event exists, Changes can omit the event. This is recorded in [known limitations](LIMITATIONS.md). The walkthrough verified the ordinary and retry paths, and did not independently reproduce this timing window.

## Retrieval baseline and next experiment

[graph.py](../tenderfit/retrieval/graph.py) builds passage links among readable documents for a single notice version. It recognizes section and appendix references, prefers same-document headings, and recognizes certain capitalized defined terms in definition sections. Generic named-document references are skipped by the edge builder. Expansion follows one hop, filters by requested labels and destination ownership, and returns up to six links. [search.py](../tenderfit/retrieval/search.py) filters lexical and vector candidates by version and supplier visibility, fuses ranks, reranks and retains explicit links. It has no lot filter. Dependency records drive the amendment planner and verdict reuse. They do not resolve evidence across versions at query time.

For the installation requirement, both assessments linked to Appendix B Definitions, page 7, with reasons citing Appendix B and the term Working Days. Joining each linked passage to its document confirmed v1 → v1 and v2 → v2. The second trace can be carried over. This check proves the observed target lineage, not an independent retrieval-quality improvement.

A real evaluation candidate is the University of Birmingham Small Works Build Higher Framework. Its corrigendum reduced public liability cover from £10m to £5m for all lots and retained the option to commit to obtaining cover before contract commencement. A fictional supplier with a current £5m policy and no recorded commitment to increase it can exercise both the threshold and the need to ask about commitment. [Original notice](https://www.find-tender.service.gov.uk/Notice/031065-2023) · [Published correction, section VII](https://www.find-tender.service.gov.uk/Notice/031188-2023)

For the first independently labelled case, freeze both releases and source packs, record document hashes, lot, page, quote, version and supplier evidence date, and label the old clause, replacement and unaffected controls. Include a second case where only a referenced definition changes. Replay v1 after importing v2. Compare plain hybrid retrieval, same-version graph expansion, targeted reassessment and full reassessment on the same inputs. Inspect the actual quoted passage, source version, effective fields, supplier fact and complete set of verdicts per lot before reporting aggregate scores. The Birmingham case is a candidate. This baseline did not run it through extraction or establish a real-document benchmark.

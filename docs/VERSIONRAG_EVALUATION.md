# VersionRAG pilot evaluation

**The clearest measured benefit was correcting stale assessments.** With the production encoders, hybrid search already found the relevant timetable. After an amendment, both baseline arms retained the old positive verdict. VersionRAG reopened it and matched a fresh assessment. A one-hop ablation with the new dependency recording and carry policy also succeeded. This experiment does not establish that two-hop expansion alone improves production retrieval.

## Experiment arms and sources

B1 uses the existing hybrid ranker and guards. The one-hop comparator adds the existing graph and corrective search with the agent disabled. VersionRAG adds version-pinned structured inputs, explicit two-hop traversal, provenance and a more conservative carry policy. The ablation retains the new recording and carry policy while withholding second-hop links from search on the timetable fixture. It still runs the resolver's gap checks. Each arm sees the same supplier, documents, clock and scripted model or fixed parser.

The production run used PostgreSQL 16 with pgvector, the cached all-MiniLM-L6-v2 embedder (1110a243) and ms-marco-MiniLM-L-6-v2 cross-encoder (233902d2). A separate quick run used hashed tokens and lexical overlap. Model answers in the synthetic cases were scripted. The real insurance probe used a fixed parser for question 82. No external generative model was called. The experiments used the repository's Python 3.12 environment. [Dataset and reproduction commands](../eval/real/fts-2023/README.md)

## Real notices

We verified the archived XML hashes and projected source fields for 48 notices from 24 procurements. Twenty deadline pairs and one insurance pair were executed. Three additional pairs carry annotated scope or negative controls. These are selected passages, not exhaustively labelled tender packs.

| Measure | Existing hybrid | VersionRAG | Unit |
|---|---:|---:|---|
| Deadline passage retrieved at rank 1 | 40/40 | 40/40 | Two snapshots for each of 20 pairs, small notice-only candidate pools |
| Correct before/after deadline field change | 20/20 | 20/20 | Existing field diff against annotated original and replacement dates |
| Correct date-only readiness outcome | 20/40 | 40/40 | Both snapshots assessed after the amendment had arrived |
| False MET on date-only readiness | 17/20 | 0/20 | Negative reference outcomes, including the earlier deadline of each extension |
| Correct current-version readiness | 20/20 | 20/20 | One latest snapshot per pair |

The date probe pairs the actual application context lookup with an external date-only readiness check. It tests historical snapshot selection. There was no retrieval improvement on this task. The supplier readiness date was intentionally selected between deadlines, so this sample cannot estimate an operational false-positive rate. Times within a closing day, intervening versions and other obligations fall outside the date probe.

**Birmingham Small Works Build Higher Framework.** The original notice links to a questionnaire. Its published correction reproduces the old and new question 82. The public liability threshold fell from £10m to £5m and retained an option to commit to obtaining cover. The fixture supplier has documented £5m cover and declines any increase. A fixed parser extracted the correct threshold in 2/2 versions. All three arms returned Not met and then Met. Both verdicts cited a matching quote from the correct snapshot. False MET was 0/1 per arm. The application classified the change to participation conditions as structural and performed full reassessment. The overall tender remained unassessed beyond relevance because the pack was incomplete. [Original notice](https://www.find-tender.service.gov.uk/Notice/031065-2023) · [Published correction](https://www.find-tender.service.gov.uk/Notice/031188-2023)

This replay projects the published text into two local notice snapshots and uses one illustrative lot. The earlier questionnaire text comes retrospectively from the correction. The supplier's refusal to increase cover is a scenario restriction. The fixed parser alone cannot judge a supplier whose willingness is unknown. The 2/2 result applies to this annotated insurance component only.

## Synthetic two-hop amendment

The buyer's installation clause refers to Appendix B, which points to Schedule C for working days. Schedule C changes from weekdays to weekends. The submission deadline also passes the supplier's insurance expiry. Of nine assessed requirements, two should change status. State comparisons below use matched supplier, lot and requirement IDs in the same snapshot; missing and extra IDs count as differences.

| Production encoder measure | B1 hybrid | Existing one-hop | One-hop read-set ablation | VersionRAG |
|---|---:|---:|---:|---:|
| Relevant dependency passages in recorded contexts, both versions | 4/4 | 4/4 | 4/4 | 4/4 |
| Relevant dependency passages / distinct retrieved passages | 4/10 | 4/10 | 4/10 | 4/10 |
| Correct installation verdicts, original and amended | 1/2 | 1/2 | 2/2 | 2/2 |
| False MET on the amended installation requirement | 1/1 | 1/1 | 0/1 | 0/1 |
| Requirements reopened | 3/9 | 3/9 | 5/9 | 5/9 |
| Reassessment precision, changed statuses / reopened | 1/3 | 1/3 | 2/5 | 2/5 |
| Reassessment recall, changed statuses reopened | 1/2 | 1/2 | 2/2 | 2/2 |
| All targeted statuses agree with a full v2 run | No | No | Yes | Yes |

The context metric includes carried retrieval records. A record naming the right passage in the amended assessment does not establish that the decision was recomputed. The scripted model cited its requirement source. The baseline planner missed the changed timetable because the retrieved timetable was absent from the verdict's read dependencies. VersionRAG also records the consulted context. It resolved the path to the effective timetable, recorded the predecessor passage and made both accessible in the evidence panel.

**Cost and regression.** VersionRAG reopened two more requirements than the baseline under production encoders. One of the extra checks corrected a missed change. Three of its five reopened verdicts kept their status. This is deliberate over-reassessment on this fixture; cost, latency and precision at real tender-pack scale remain unknown.

With the lightweight encoders, both baseline arms found Appendix B but missed Schedule C in v1, and returned Needs confirmation too early. Two-hop VersionRAG recovered the timetable and produced the correct weekday and weekend outcomes. This demonstrates the traversal when ranking misses the target. The production experiment found no increase in passage recall.

## Extraction, citations and limits

On the independent, synthetic Northfield fixture all three main arms had mandatory extraction recall 1.0, extraction precision 1.0, verdict macro-F1 1.0, false MET rate 0 and quote-match rate 1.0 with scripted extraction/verdict responses. This is a regression control. Quote matching only establishes that the cited words exist. The timetable example illustrates the distinction: the model's own citation names the source requirement, while the extra path in the trace identifies the timetable needed to justify it. Independent citation entailment and real generative extraction accuracy remain unmeasured.

The corpus is weighted toward deadline corrections and has no held-out test partition or human-reviewed agreement score. The three additional labelled cases have no system scores. Same-version contradiction checks are numeric and lexical, predecessor links depend on passage alignment, passage ownership does not generally identify lot applicability, and supplier facts are not historical snapshots. Named-document resolution, deeper chains and precedence between conflicting active documents require more evidence before broader claims.

For the professor demonstration, use the installation example to show the retrieved path, the change to a passage outside the requirement, and the resulting verdict. Use the real Birmingham source alongside it to show the exact correction and the boundary of the measured result. [Demo walkthrough](VERSIONRAG_DEMO.md)

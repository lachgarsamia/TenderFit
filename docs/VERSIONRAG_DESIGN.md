# VersionRAG experimental design

## Hypothesis

When a requirement cites an appendix which in turn defines a term elsewhere, explicitly resolving both hops within the requested release should recover supporting passages missed by one-hop retrieval. An amendment to the downstream definition should reopen the dependent verdict even when the requirement sentence is unchanged. More context can also lower precision or force unnecessary abstentions. Measure both.

## Proposed architecture

| Component | Purpose and boundary |
|---|---|
| Snapshot context | Resolve the requested release within the lot's procurement. Use the frozen lot and notice fields for dates, geography and quantity. Supplier facts and the assessment clock remain explicit inputs. |
| Bounded dependency traversal | Reuse passage_edge. Start from the requirement's own quoted references and extracted terms. Resolve up to two hops to visible, readable passages within one version. Record each path and unresolved or ambiguous references. Treat cycles and duplicate paths explicitly. |
| Evidence and abstention | Add resolved passages to existing hybrid candidates. Keep the original retrieval ranking available. A positive decision with unresolved explicit evidence needs human confirmation. Use the existing same-version conflict check. A superseded clause is historical evidence of change. It cannot by itself contradict the effective clause. |
| Amendment processing | Reuse the release diff, targeted planner and per-verdict reads. Reresolve explicit dependencies when moving to a new snapshot, including previously missing targets. Rebuild provenance from the target passage, including document identity, version, section and page. |
| Comparison | Keep all previous experiment arms and defaults available. Opt in with an explicit setting or evaluation arm. Compare identical fixtures, suppliers and assessment clocks. Keep the lot in comparison keys and compare both directions. |

All components can use existing PostgreSQL tables, pgvector and verdict JSON. No graph database or second vector store is needed. Two hops bound the experiment and its interpretability. General precedence between simultaneously active documents, legal interpretation of every corrigendum, arbitrary graph traversal and historical snapshots of company facts remain research questions.

## Evidence plan

Use one scripted two-hop case to isolate retrieval mechanics and the existing multi-lot demo to verify the complete workflow. Separately freeze official releases, annotate changed fields or clauses and source locators, define hypothetical supplier facts, and distinguish author-reviewed labels from independent procurement review. Report retrieval recall and precision, extraction, verdict agreement, false MET, citation correctness, change detection and the fraction of reopened requirements that actually changed. State the unit, denominator and missing measurements. Use an unchanged one-hop arm as the strongest comparator. Keep incomplete document packs and deadline-only cases identifiable.

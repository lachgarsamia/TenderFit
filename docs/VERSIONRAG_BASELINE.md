# Retrieval baseline at be56872

A raw release is keyed by procurement and release ID. Normalisation materialises notice fields and lots per notice version. Opportunity and lot rows also expose mutable current values. Documents belong to a version and can belong to a supplier. Passages identify their document, ordinal, first page, section, text hash, full-text index and embedding. Requirements identify their version, lot, source passage and quote. Verdicts join one assessment to a requirement, with separate citation, fact and dependency rows.

## Trace through the implementation

1. The importer stores a notice snapshot and updates the current pointer. The document service fetches or accepts uploads, parses readable content and records coverage. Extraction validates quotes and structured conditions against passages.
2. retrieval/search.py queries PostgreSQL full text and pgvector for the requested version and visible documents. Reciprocal rank fusion combines candidate lists. A cross-encoder reranks candidates. The requirement's source passage is included even if search misses it.
3. retrieval/graph.py recognises explicit section and appendix mentions, prefers same-document targets and links certain two-word capitalised terms to passages in definition sections. It inserts REFERENCES and DEFINED_BY edges within a single version. Expansion uses labels from the requirement, filters target ownership and takes six one-hop links. Linked candidates survive reranking. Named documents are skipped by the edge builder.
4. assessment/pipeline.py applies deterministic rules, model citation and evidence guards, a corrective search, and a numeric contradiction heuristic. Rules can settle a requirement before model retrieval. An optional agent can investigate. Neither full-text relevance nor a valid verbatim citation establishes entailment.
5. changes/apply.py compares releases, aligns passages by hash and fuzzy matching, and copies requirements whose source stayed stable. changes/impact.py intersects changed passages, fields, facts and referenced documents with the assessment's dependencies. Structural or ambiguous changes trigger full reassessment. Changed passages are re-extracted. Remaining verdicts can be carried across with remapped links.

## Research gaps

Search is scoped to a version, but graph expansion cannot follow an appendix onward to a definition. A top-six result can hide an ambiguous target. A new target may have no previous passage dependency. Lot scope is applied to requirements but passages lack reliable lot ownership. The graph uses heading and capitalisation heuristics. OCR gaps and inaccessible originals limit coverage. Existing conflict detection compares similar clauses with differing numbers within a version. The published version history alone cannot decide which of two conflicting active documents governs.

Historical replay at the baseline mixed old documents with current structured fields. Carry-over could retain an obsolete display filename. The synthetic comparator can collapse identical statements across lots. The optional agent, scripted tests and prior synthetic results do not establish real-notice accuracy. See [executed baseline](PRE_MERGE_BASELINE.md).

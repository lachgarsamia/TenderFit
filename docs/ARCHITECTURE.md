# Architecture

## Components

```mermaid
flowchart TB
  FTS[Find a Tender OCDS API]
  Portal[Public attachments]
  Model[Anthropic API]
  subgraph Web["web: React, TypeScript, Vite, served by nginx"]
    Pages[Opportunities, Tender, Assessment, Changes, Profile, Preparation]
  end
  subgraph Api["tenderfit/api: FastAPI"]
    Routes[Routes, upload limits, profile scoping]
  end
  subgraph Worker["tenderfit/jobs: worker on a Postgres queue"]
    Jobs[import, assess, detect_changes, shadow_full, profile_change, daily_check]
  end
  subgraph Core["Domain modules"]
    Ingest[ingest]
    Docs[documents]
    Extract[extraction + validate]
    Retr[retrieval + graph]
    Pipe[assessment]
    Rules[rules]
    Agent[agent]
    Changes[changes]
    Profile[profile + verify]
  end
  DB[(PostgreSQL 16: pgvector, full-text, typed edges)]
  Files[(Document volume)]
  Pages -->|/api| Routes
  Routes --> DB
  Routes -->|enqueue| DB
  DB -->|claim| Jobs
  Jobs --> Core
  Ingest --> FTS
  Docs -->|public addresses only| Portal
  Docs --> Files
  Extract --> Model
  Pipe --> Model
  Agent --> Model
  Core --> DB
```

There is one database. The graph is a table in it, not a separate service: every edge is one hop within one notice version, which needs no graph engine.

## Assessment pipeline

```mermaid
flowchart TD
  A[Open a run. The last completed assessment stays current] --> B[Fetch documents, index, build passage graph]
  B --> C[Extract requirements with the model]
  C --> V{Validate}
  V -->|quote or number not in source, unknown lot| R[Rejected, kept and shown]
  V -->|condition not traceable to the quote| D[Keep requirement, drop the machine check]
  V -->|ok| E[Atomic conditions]
  D --> E
  E --> F{Each condition: rule can judge it?}
  F -->|yes| G[Deterministic result]
  F -->|no| H[Retrieve: lexical + vector + named references, RRF, rerank]
  H --> I{Reference unresolved?}
  I -->|yes| J[Second search within budget]
  J --> K
  I -->|no| K[Model reads open conditions only]
  K --> L[Guard: citations exist, facts current, no unsupported MET or NOT MET]
  L --> M{Still uncertain and agent enabled?}
  M -->|yes| N[Read-only agent, bounded by steps, cost, time]
  M -->|no| O
  N --> L2[Same guard]
  L2 --> O
  G --> O[Combine: rules bind the model. All mandatory parts must be met]
  O --> P[Unread reference? Documents disagree? Lower to needs confirmation]
  P --> Q[Evidence quality, dependencies, citations]
  Q --> S[Commit: new assessment becomes current in one transaction]
```

### Verdict policy

| Verdict | Requires |
|---|---|
| **Met** | Every mandatory condition satisfied by a current, compatible fact. Proof where the buyer asks for it |
| **Not met** | An explicit, current, relevant fact contradicts it. A contract list omitting something is not contradiction |
| **Needs confirmation** | Support exists but must be confirmed, or the facts or the documents disagree |
| **Unknown** | Nothing in the profile or documents settles it |

Evidence quality (`strong`, `weak`, `contradictory`, `missing`, `ambiguous`) is a reason, not a probability. No confidence percentage is shown, because none is calibrated.

## Amendment processing

```mermaid
stateDiagram-v2
  [*] --> detected: new version, current assessments behind it
  detected --> processing
  processing --> applied: every lot and profile succeeded
  processing --> failed: anything failed. Finished pairs stay finished
  failed --> processing: automatic retry with back-off, or Retry now
  failed --> superseded: a newer notice replaced the target
  detected --> superseded
```

Requirement preparation for a version pair is one transaction ending in a durable marker, so a failed extraction leaves nothing to clean up. Each impact is written in the same transaction as the assessment it describes. Only one process applies amendments at a time, through an advisory lock.

Dependencies are recorded per verdict: what extraction found (source, references, definitions, parameters) and what the verdict used (facts, rule parameters, passages read, the deadline where a dated fact is involved). A plan for one assessment reads only its own verdicts' edges. Structural or ambiguous changes reassess everything.

## Data model

```mermaid
erDiagram
  opportunity ||--o{ notice_version : has
  opportunity ||--o{ lot : has
  notice_version ||--o{ document : has
  notice_version ||--o{ document_ref : lists
  document ||--o{ passage : splits_into
  passage ||--o{ passage_edge : links
  notice_version ||--o{ requirement : yields
  requirement ||--o{ requirement_condition : decomposes_into
  requirement ||--o{ dependency : depends_on
  business_profile ||--o{ profile_fact : has
  business_profile ||--o{ evidence_file : owns
  profile_fact ||--o{ fact_support : proven_by
  profile_fact ||--o{ fact_verification : checked_by
  assessment ||--o{ verdict : contains
  verdict ||--o{ verdict_condition : details
  verdict ||--o{ verdict_citation : cites
  verdict ||--o{ verdict_fact : uses
  verdict ||--o{ question : asks
  change_event ||--o{ change_impact : causes
  document_ref ||--o{ document_ref_state : per_supplier
```

Migrations are numbered SQL files applied in order: 001 to 016.

**Identity:** `tenderfit/auth` holds accounts (Argon2id), sessions (random tokens stored only as SHA-256) and membership. `tenderfit/api/deps.py` derives the user and active supplier for each request and re-checks membership each time; no route takes a supplier or user from the client. Another supplier's resource returns 404. State-changing requests need an `X-TenderFit` header, which a cross-site form cannot send. Workers run as the supplier named on the job, set by the API from the session.

**Replay:** `replay` is non-destructive. `destructive_rebuild` is reachable only from the command line with two explicit flags and the exact database name; no API route or worker job reaches it.

**Isolation:** public tender data has no owner. A supplier's uploads, requirements extracted from them, and decisions about documents carry `owner_profile_id` or live in `document_ref_state`. Search, graph expansion, inventory, assessment and source viewing all filter on it.

## Guarantees and the tests behind them

| Guarantee | Tests |
|---|---|
| A failure at any stage leaves the last good assessment readable | `test_assessment_lifecycle.py` |
| An amendment counts as applied only when finished; retries leave no duplicates or losses | `test_amendment_retry.py` |
| Fact order never changes a verdict; missing information is never NOT MET; bad conditions never raise | `test_rules_regression.py` (property tests) |
| A number the model invents never becomes a rule input | `test_extraction_validation.py` |
| One supplier cannot affect another | `test_isolation.py` |
| Targeted reassessment reaches changes on controlled cases, including across amendments | `test_soundness.py`, `tenderfit/eval/regression.py` |

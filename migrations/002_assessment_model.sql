CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE document (
    id bigserial PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES notice_version (id),
    url text,
    sha256 text,
    origin text NOT NULL CHECK (origin IN ('source', 'upload')),
    access text NOT NULL CHECK (access IN ('read', 'unreadable', 'not_retrieved')),
    retrieved_at timestamptz
);

CREATE TABLE document_ref (
    id bigserial PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES notice_version (id),
    label text NOT NULL,
    ocds_type text,
    url text,
    referenced_in text,
    document_id bigint REFERENCES document (id),
    essential boolean NOT NULL DEFAULT true
);

CREATE TABLE passage (
    id bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES document (id),
    page int,
    section text,
    text text NOT NULL,
    sha256 text NOT NULL,
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384)
);
CREATE INDEX passage_tsv ON passage USING gin (tsv);

CREATE TABLE requirement (
    id bigserial PRIMARY KEY,
    lot_id bigint REFERENCES lot (id),
    version_id bigint NOT NULL REFERENCES notice_version (id),
    statement text NOT NULL,
    category text NOT NULL,
    obligation text NOT NULL CHECK (obligation IN ('mandatory', 'scored', 'informational')),
    condition jsonb,
    equivalent_allowed boolean NOT NULL DEFAULT false,
    evidence_needed text[] NOT NULL DEFAULT '{}',
    quote text NOT NULL,
    passage_id bigint REFERENCES passage (id)
);

CREATE TABLE business_profile (
    id bigserial PRIMARY KEY,
    name text NOT NULL,
    regions text[] NOT NULL DEFAULT '{}',
    is_fictional boolean NOT NULL DEFAULT true
);

CREATE TABLE profile_fact (
    id bigserial PRIMARY KEY,
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    attribute text NOT NULL,
    value jsonb NOT NULL,
    evidence_status text NOT NULL CHECK (evidence_status IN ('document_on_file', 'declared', 'missing', 'unknown', 'needs_update')),
    source text,
    confirmed_at timestamptz,
    valid_until date,
    confirm_by date
);

CREATE TABLE evidence_file (
    id bigserial PRIMARY KEY,
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    kind text NOT NULL,
    issuer text,
    issued_on date,
    expires_on date,
    sha256 text NOT NULL
);

CREATE TABLE fact_support (
    id bigserial PRIMARY KEY,
    fact_id bigint NOT NULL REFERENCES profile_fact (id),
    evidence_file_id bigint NOT NULL REFERENCES evidence_file (id),
    page int,
    quote text
);

CREATE TABLE dependency (
    id bigserial PRIMARY KEY,
    requirement_id bigint NOT NULL REFERENCES requirement (id),
    version_id bigint NOT NULL REFERENCES notice_version (id),
    kind text NOT NULL CHECK (kind IN ('source', 'reference', 'definition', 'parameter', 'read', 'fact')),
    target_type text NOT NULL CHECK (target_type IN ('passage', 'field', 'document', 'fact')),
    target_id text NOT NULL,
    recorded_by text NOT NULL CHECK (recorded_by IN ('extractor', 'rule_engine', 'agent_trace'))
);
CREATE INDEX dependency_target ON dependency (target_type, target_id);

CREATE TABLE assessment (
    id bigserial PRIMARY KEY,
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    lot_id bigint NOT NULL REFERENCES lot (id),
    version_id bigint NOT NULL REFERENCES notice_version (id),
    recommendation text,
    coverage text CHECK (coverage IN ('complete', 'partial', 'notice_only')),
    provisional boolean NOT NULL DEFAULT true,
    mode text CHECK (mode IN ('full', 'targeted')),
    full_reason text,
    superseded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE verdict (
    id bigserial PRIMARY KEY,
    assessment_id bigint NOT NULL REFERENCES assessment (id),
    requirement_id bigint NOT NULL REFERENCES requirement (id),
    status text NOT NULL CHECK (status IN ('met', 'not_met', 'needs_confirmation', 'unknown')),
    reason text NOT NULL,
    basis text CHECK (basis IN ('documents', 'declared', 'rule')),
    needs_human text,
    provisional boolean NOT NULL DEFAULT false,
    carried_over boolean NOT NULL DEFAULT false
);

CREATE TABLE verdict_citation (
    verdict_id bigint NOT NULL REFERENCES verdict (id),
    passage_id bigint NOT NULL REFERENCES passage (id),
    quote text NOT NULL,
    PRIMARY KEY (verdict_id, passage_id)
);

CREATE TABLE verdict_fact (
    verdict_id bigint NOT NULL REFERENCES verdict (id),
    fact_id bigint NOT NULL REFERENCES profile_fact (id),
    PRIMARY KEY (verdict_id, fact_id)
);

CREATE TABLE question (
    id bigserial PRIMARY KEY,
    verdict_id bigint NOT NULL REFERENCES verdict (id),
    text text NOT NULL,
    answer text,
    answered_at timestamptz
);

CREATE TABLE confirmation (
    id bigserial PRIMARY KEY,
    point text NOT NULL CHECK (point IN ('G1', 'G2', 'G3', 'G4', 'G5', 'G6', 'G7')),
    user_name text NOT NULL,
    target_type text NOT NULL,
    target_id bigint NOT NULL,
    decision text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE requirement
    ADD COLUMN lot_no text,
    ADD COLUMN section text,
    ADD COLUMN page int,
    ADD COLUMN refs text[] NOT NULL DEFAULT '{}',
    ADD COLUMN defined_terms text[] NOT NULL DEFAULT '{}',
    ADD COLUMN quote_score real,
    ADD COLUMN created_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX requirement_version ON requirement (version_id);

CREATE TABLE requirement_rejection (
    id bigserial PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES notice_version (id),
    passage_id bigint REFERENCES passage (id),
    statement text,
    quote text,
    score real,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE llm_call (
    id bigserial PRIMARY KEY,
    role text NOT NULL,
    model text NOT NULL,
    prompt_version text NOT NULL,
    input_tokens int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    cache_read_tokens int NOT NULL DEFAULT 0,
    cost_usd numeric(10, 6) NOT NULL DEFAULT 0,
    latency_ms int NOT NULL DEFAULT 0,
    stop_reason text,
    refs jsonb NOT NULL DEFAULT '{}',
    error text,
    created_at timestamptz NOT NULL DEFAULT now()
);

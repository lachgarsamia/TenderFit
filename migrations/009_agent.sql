CREATE TABLE agent_step (
    id bigserial PRIMARY KEY,
    assessment_id bigint NOT NULL REFERENCES assessment (id),
    requirement_id bigint NOT NULL REFERENCES requirement (id),
    step int NOT NULL,
    tool text NOT NULL,
    input jsonb NOT NULL,
    output jsonb NOT NULL,
    input_tokens int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    cost_usd numeric(10, 6) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX agent_step_assessment ON agent_step (assessment_id, requirement_id, step);

ALTER TABLE verdict DROP CONSTRAINT verdict_decided_by_check,
    ADD CONSTRAINT verdict_decided_by_check CHECK (decided_by IN ('rule', 'model', 'budget', 'guard', 'agent'));

ALTER TABLE verdict_citation DROP CONSTRAINT verdict_citation_pkey, ADD PRIMARY KEY (verdict_id, passage_id, quote);

ALTER TABLE assessment
    ADD COLUMN dimensions jsonb,
    ADD COLUMN status text NOT NULL DEFAULT 'done' CHECK (status IN ('running', 'done', 'failed')),
    ADD COLUMN cost_usd numeric(10, 6) NOT NULL DEFAULT 0,
    ADD COLUMN model_calls int NOT NULL DEFAULT 0,
    ADD COLUMN budget_reached boolean NOT NULL DEFAULT false,
    ADD COLUMN error text,
    ADD COLUMN finished_at timestamptz;
CREATE INDEX assessment_lot ON assessment (lot_id, profile_id, created_at DESC);

ALTER TABLE verdict
    ADD COLUMN question text,
    ADD COLUMN escalate boolean NOT NULL DEFAULT false,
    ADD COLUMN escalate_reason text,
    ADD COLUMN decided_by text NOT NULL DEFAULT 'rule' CHECK (decided_by IN ('rule', 'model', 'budget', 'guard')),
    ADD COLUMN override_status text,
    ADD COLUMN override_reason text,
    ADD COLUMN override_by text;

ALTER TABLE dependency DROP CONSTRAINT dependency_recorded_by_check,
    ADD CONSTRAINT dependency_recorded_by_check CHECK (recorded_by IN ('extractor', 'rule_engine', 'agent_trace', 'verdict'));
ALTER TABLE dependency ADD COLUMN verdict_id bigint REFERENCES verdict (id);

CREATE TABLE lot_fact_confirmation (
    id bigserial PRIMARY KEY,
    lot_id bigint NOT NULL REFERENCES lot (id),
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    fact_id bigint NOT NULL REFERENCES profile_fact (id),
    kind text NOT NULL CHECK (kind IN ('workload', 'declaration')),
    confirmed_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (lot_id, profile_id, fact_id, kind)
);

CREATE TABLE verdict_cache (
    key text PRIMARY KEY,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE lot_decision (
    lot_id bigint NOT NULL REFERENCES lot (id),
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    decision text NOT NULL CHECK (decision IN ('pursue', 'not_for_us')),
    decided_by text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lot_id, profile_id)
);

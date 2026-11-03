-- A requirement can hold several independently assessable conditions. requirement.condition stays as the primary one.
ALTER TABLE requirement
    ADD COLUMN scope text,
    ADD COLUMN condition_note text,
    ADD COLUMN extract_version text;

CREATE TABLE requirement_condition (
    id bigserial PRIMARY KEY,
    requirement_id bigint NOT NULL REFERENCES requirement (id),
    ordinal int NOT NULL,
    label text NOT NULL,
    condition jsonb,                 -- null: not machine-checkable, the text must be interpreted
    quote text,
    mandatory boolean NOT NULL DEFAULT true,
    evidence_needed text[] NOT NULL DEFAULT '{}',
    condition_note text,             -- why a proposed machine check was not trusted
    UNIQUE (requirement_id, ordinal)
);

CREATE TABLE verdict_condition (
    verdict_id bigint NOT NULL REFERENCES verdict (id),
    condition_id bigint NOT NULL REFERENCES requirement_condition (id),
    status text NOT NULL,
    reason text NOT NULL,
    decided_by text NOT NULL,
    required text,
    observed text,
    PRIMARY KEY (verdict_id, condition_id)
);

ALTER TABLE requirement_rejection
    ADD COLUMN kind text NOT NULL DEFAULT 'quote',
    ADD COLUMN obligation text,
    ADD COLUMN lot_no text;
CREATE INDEX requirement_rejection_version ON requirement_rejection (version_id);

-- A file on record is not proof. Each check on a file's contents is stored, along with what could not be checked.
CREATE TABLE fact_verification (
    id bigserial PRIMARY KEY,
    fact_id bigint NOT NULL REFERENCES profile_fact (id),
    evidence_file_id bigint NOT NULL REFERENCES evidence_file (id),
    level text NOT NULL CHECK (level IN ('exists', 'relevant', 'supports', 'current')),
    checks jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fact_verification_fact ON fact_verification (fact_id, id DESC);
ALTER TABLE fact_support ADD COLUMN human_verified_by text, ADD COLUMN human_verified_at timestamptz;

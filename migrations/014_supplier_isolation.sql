-- Public tender data stays shared. What one supplier uploads or decides belongs to that supplier only.
ALTER TABLE document ADD COLUMN owner_profile_id bigint REFERENCES business_profile (id);
ALTER TABLE document_ref ADD COLUMN owner_profile_id bigint REFERENCES business_profile (id);
ALTER TABLE requirement ADD COLUMN owner_profile_id bigint REFERENCES business_profile (id);
CREATE INDEX document_owner ON document (owner_profile_id) WHERE owner_profile_id IS NOT NULL;
CREATE INDEX requirement_owner ON requirement (owner_profile_id) WHERE owner_profile_id IS NOT NULL;

-- A supplier's view of a shared listed document: uploaded privately, or confirmed as not issued.
CREATE TABLE document_ref_state (
    ref_id bigint NOT NULL REFERENCES document_ref (id),
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    status text NOT NULL CHECK (status IN ('read', 'unreadable', 'confirmed_none')),
    document_id bigint REFERENCES document (id),
    note text,
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ref_id, profile_id)
);

ALTER TABLE question ADD COLUMN profile_id bigint REFERENCES business_profile (id);
UPDATE question q SET profile_id = a.profile_id
FROM verdict v JOIN assessment a ON a.id = v.assessment_id WHERE v.id = q.verdict_id;
CREATE INDEX question_requirement ON question (requirement_id, profile_id);

-- A workload or declaration confirmation holds for one notice version; an amendment asks again.
ALTER TABLE lot_fact_confirmation ADD COLUMN version_id bigint REFERENCES notice_version (id);
ALTER TABLE lot_fact_confirmation DROP CONSTRAINT lot_fact_confirmation_lot_id_profile_id_fact_id_kind_key;
CREATE UNIQUE INDEX lot_fact_confirmation_once ON lot_fact_confirmation (lot_id, profile_id, fact_id, kind, coalesce(version_id, 0));

CREATE INDEX dependency_verdict ON dependency (verdict_id);
CREATE INDEX dependency_version ON dependency (version_id);
CREATE INDEX verdict_fact_fact ON verdict_fact (fact_id);
CREATE INDEX notice_version_opportunity ON notice_version (opportunity_id);

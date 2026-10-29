ALTER TABLE confirmation
    ADD COLUMN profile_id bigint REFERENCES business_profile (id),
    ADD COLUMN payload jsonb NOT NULL DEFAULT '{}';
CREATE INDEX confirmation_target ON confirmation (target_type, target_id);

ALTER TABLE question ADD COLUMN answered_by text, ADD COLUMN requirement_id bigint REFERENCES requirement (id);

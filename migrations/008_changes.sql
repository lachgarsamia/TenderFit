CREATE TABLE change_event (
    id bigserial PRIMARY KEY,
    opportunity_id bigint NOT NULL REFERENCES opportunity (id),
    kind text NOT NULL CHECK (kind IN ('version', 'status', 'profile', 'upload')),
    from_version_id bigint REFERENCES notice_version (id),
    to_version_id bigint REFERENCES notice_version (id),
    cls text NOT NULL CHECK (cls IN ('local', 'structural', 'ambiguous', 'external', 'status')),
    reasons text[] NOT NULL DEFAULT '{}',
    fields jsonb NOT NULL DEFAULT '[]',
    passages jsonb NOT NULL DEFAULT '[]',
    documents jsonb NOT NULL DEFAULT '[]',
    summary jsonb NOT NULL DEFAULT '{}',
    detected_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX change_event_version ON change_event (from_version_id, to_version_id) WHERE kind = 'version';
CREATE UNIQUE INDEX change_event_status ON change_event (opportunity_id, cls, (summary->>'status')) WHERE kind = 'status';

CREATE TABLE change_impact (
    id bigserial PRIMARY KEY,
    change_id bigint NOT NULL REFERENCES change_event (id),
    lot_id bigint NOT NULL REFERENCES lot (id),
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    before_assessment_id bigint REFERENCES assessment (id),
    after_assessment_id bigint REFERENCES assessment (id),
    shadow_assessment_id bigint REFERENCES assessment (id),
    mode text NOT NULL,
    reason text,
    affected jsonb NOT NULL DEFAULT '[]',
    requirements_added jsonb NOT NULL DEFAULT '[]',
    requirements_removed jsonb NOT NULL DEFAULT '[]',
    kept int NOT NULL DEFAULT 0,
    explanation text,
    audit jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE assessment ADD COLUMN shadow boolean NOT NULL DEFAULT false;
ALTER TABLE requirement ADD COLUMN carried_from bigint REFERENCES requirement (id);

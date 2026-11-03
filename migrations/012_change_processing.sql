-- An amendment is applied only when its processing has finished. Earlier events had no state and are final.
ALTER TABLE change_event
    ADD COLUMN status text NOT NULL DEFAULT 'applied'
        CHECK (status IN ('detected', 'processing', 'applied', 'failed', 'superseded')),
    ADD COLUMN attempts int NOT NULL DEFAULT 0,
    ADD COLUMN last_error text,
    ADD COLUMN started_at timestamptz,
    ADD COLUMN finished_at timestamptz;
ALTER TABLE change_event ALTER COLUMN status SET DEFAULT 'detected';

CREATE UNIQUE INDEX change_impact_once ON change_impact (change_id, lot_id, profile_id);

-- Durable marker: requirements of the new version are ready for this version pair.
CREATE TABLE version_preparation (
    from_version_id bigint NOT NULL REFERENCES notice_version (id),
    to_version_id bigint NOT NULL REFERENCES notice_version (id),
    added jsonb NOT NULL DEFAULT '[]',
    removed jsonb NOT NULL DEFAULT '[]',
    prepared_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (from_version_id, to_version_id)
);

-- A running assessment no longer hides the last good one. Only one completed, current assessment and one
-- running assessment can exist per lot and profile (shadow audits are excluded).
ALTER TABLE assessment
    ADD COLUMN error_stage text,
    ADD COLUMN job_id bigint REFERENCES job (id),
    ADD COLUMN progress jsonb NOT NULL DEFAULT '{}',
    ADD COLUMN started_at timestamptz NOT NULL DEFAULT now();

-- Runs interrupted under the old lifecycle never reached a final state.
UPDATE assessment SET status = 'failed', superseded = true, finished_at = coalesce(finished_at, now()),
       error = coalesce(error, 'interrupted before this run finished'), error_stage = 'unknown'
WHERE status = 'running';

-- Keep only the newest completed assessment current, and restore it where a failed run had superseded it.
UPDATE assessment a SET superseded = (a.id <> latest.id)
FROM (SELECT lot_id, profile_id, max(id) AS id FROM assessment
      WHERE status = 'done' AND NOT shadow GROUP BY lot_id, profile_id) latest
WHERE a.lot_id = latest.lot_id AND a.profile_id = latest.profile_id AND a.status = 'done' AND NOT a.shadow;

CREATE UNIQUE INDEX assessment_one_current ON assessment (lot_id, profile_id)
    WHERE status = 'done' AND NOT superseded AND NOT shadow;
CREATE UNIQUE INDEX assessment_one_running ON assessment (lot_id, profile_id)
    WHERE status = 'running' AND NOT shadow;
CREATE INDEX verdict_assessment ON verdict (assessment_id);
CREATE INDEX document_version ON document (version_id);
CREATE INDEX job_active_kind ON job (kind) WHERE status IN ('queued', 'running');

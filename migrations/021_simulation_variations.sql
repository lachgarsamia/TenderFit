-- Scenario variations (Mode B, phase 4). A variation is a new, isolated simulation that reuses its parent's frozen
-- snapshot exactly, so scenarios explored from one baseline are decided on identical inputs. Only simulation tables change.

ALTER TABLE simulation ADD COLUMN parent_id bigint;

-- The foreign key below proves, in the database, that a variation belongs to the same supplier, the same baseline
-- assessment and the very same snapshot as its parent. id is already unique, so this index cannot fail.
CREATE UNIQUE INDEX simulation_lineage_key ON simulation (id, profile_id, baseline_assessment_id, snapshot_sha256);
ALTER TABLE simulation ADD CONSTRAINT simulation_parent_fkey
    FOREIGN KEY (parent_id, profile_id, baseline_assessment_id, snapshot_sha256)
    REFERENCES simulation (id, profile_id, baseline_assessment_id, snapshot_sha256);
CREATE INDEX simulation_parent ON simulation (parent_id) WHERE parent_id IS NOT NULL;

-- As 019's guard, with parent_id among what a simulation is based on: it never changes after creation.
CREATE OR REPLACE FUNCTION simulation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'discarded' THEN
        RAISE EXCEPTION 'simulation % is discarded', OLD.id;
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN (
            ('created', 'running'), ('created', 'discarded'), ('running', 'completed'), ('running', 'failed'),
            ('completed', 'discarded'), ('failed', 'discarded')) THEN
        RAISE EXCEPTION 'simulation % cannot go from % to %', OLD.id, OLD.status, NEW.status;
    END IF;
    IF (NEW.profile_id, NEW.lot_id, NEW.baseline_assessment_id, NEW.version_id, NEW.as_of, NEW.snapshot, NEW.snapshot_sha256,
        NEW.drift, NEW.engine_version, NEW.created_by, NEW.created_at, NEW.parent_id)
       IS DISTINCT FROM
       (OLD.profile_id, OLD.lot_id, OLD.baseline_assessment_id, OLD.version_id, OLD.as_of, OLD.snapshot, OLD.snapshot_sha256,
        OLD.drift, OLD.engine_version, OLD.created_by, OLD.created_at, OLD.parent_id) THEN
        RAISE EXCEPTION 'what simulation % is based on cannot change', OLD.id;
    END IF;
    RETURN NEW;
END $$;

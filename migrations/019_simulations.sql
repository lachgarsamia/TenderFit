-- Hypothetical experiments on a frozen copy of one assessment's inputs. Nothing here is read by the live assessment code,
-- and no live table gains a row or column: simulation state lives only in these tables, which point at live data, never
-- the other way round.

-- Lets a simulation prove, through its foreign key, that its baseline belongs to the supplier, lot and version it names.
-- id is already unique, so this cannot fail and changes no behaviour.
CREATE UNIQUE INDEX assessment_baseline_key ON assessment (id, profile_id, lot_id, version_id);

CREATE TABLE simulation (
    id bigserial PRIMARY KEY,
    profile_id bigint NOT NULL REFERENCES business_profile (id),
    lot_id bigint NOT NULL REFERENCES lot (id),
    baseline_assessment_id bigint NOT NULL,
    version_id bigint NOT NULL REFERENCES notice_version (id),
    status text NOT NULL DEFAULT 'created' CHECK (status IN ('created', 'running', 'completed', 'failed', 'discarded')),
    title text CHECK (length(title) <= 500),
    as_of date NOT NULL,
    snapshot jsonb NOT NULL,          -- facts, requirements, conditions and context fields the simulation is decided on
    snapshot_sha256 text NOT NULL,
    drift jsonb NOT NULL DEFAULT '{}', -- what changed in the live profile since the baseline ran
    engine_version text NOT NULL,
    summary jsonb,
    error text,
    error_stage text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    discarded_at timestamptz,
    FOREIGN KEY (baseline_assessment_id, profile_id, lot_id, version_id)
        REFERENCES assessment (id, profile_id, lot_id, version_id)
);
CREATE INDEX simulation_profile ON simulation (profile_id, created_at DESC);
CREATE INDEX simulation_baseline ON simulation (baseline_assessment_id);

CREATE TABLE simulation_change (
    id bigserial PRIMARY KEY,
    simulation_id bigint NOT NULL REFERENCES simulation (id) ON DELETE CASCADE,
    ordinal int NOT NULL CHECK (ordinal >= 1),
    kind text NOT NULL CHECK (kind IN ('set_condition_bound', 'set_condition_match', 'set_fact_validity', 'set_fact_evidence',
                                       'remove_fact', 'set_fact_value', 'add_fact', 'set_notice_field', 'set_requirement_text')),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (simulation_id, ordinal)
);

CREATE TABLE simulation_result (
    simulation_id bigint NOT NULL REFERENCES simulation (id) ON DELETE CASCADE,
    requirement_id bigint NOT NULL REFERENCES requirement (id),
    baseline_verdict_id bigint REFERENCES verdict (id),
    baseline_status text CHECK (baseline_status IN ('met', 'not_met', 'needs_confirmation', 'unknown')),
    control_status text CHECK (control_status IN ('met', 'not_met', 'needs_confirmation', 'unknown')),
    simulated_status text CHECK (simulated_status IN ('met', 'not_met', 'needs_confirmation', 'unknown')),
    how text NOT NULL CHECK (how IN ('rule', 'needs_model', 'not_affected')),
    simulated jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (simulation_id, requirement_id)
);

-- Lifecycle: only these transitions exist, a discarded simulation is final, and what it was decided on never changes.
CREATE FUNCTION simulation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
        NEW.drift, NEW.engine_version, NEW.created_by, NEW.created_at)
       IS DISTINCT FROM
       (OLD.profile_id, OLD.lot_id, OLD.baseline_assessment_id, OLD.version_id, OLD.as_of, OLD.snapshot, OLD.snapshot_sha256,
        OLD.drift, OLD.engine_version, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'what simulation % is based on cannot change', OLD.id;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER simulation_lifecycle BEFORE UPDATE ON simulation FOR EACH ROW EXECUTE FUNCTION simulation_guard();

-- Hypotheses can be added only before a run; results only during one. Neither is ever edited afterwards.
CREATE FUNCTION simulation_child_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    wanted text := TG_ARGV[0];
    actual text;
BEGIN
    SELECT status INTO actual FROM simulation WHERE id = NEW.simulation_id FOR SHARE;
    IF actual IS DISTINCT FROM wanted THEN
        RAISE EXCEPTION '% rows can only be added while the simulation is %, not %', TG_TABLE_NAME, wanted, coalesce(actual, 'missing');
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER simulation_change_open BEFORE INSERT ON simulation_change
    FOR EACH ROW EXECUTE FUNCTION simulation_child_guard('created');
CREATE TRIGGER simulation_change_immutable BEFORE UPDATE ON simulation_change
    FOR EACH ROW EXECUTE FUNCTION forbid_update();
CREATE TRIGGER simulation_result_open BEFORE INSERT ON simulation_result
    FOR EACH ROW EXECUTE FUNCTION simulation_child_guard('running');
CREATE TRIGGER simulation_result_immutable BEFORE UPDATE ON simulation_result
    FOR EACH ROW EXECUTE FUNCTION forbid_update();

-- The trace of the agent that answers a what-if question by building and running one simulation. It lives with the
-- simulation, not in agent_step or llm_call: nothing a simulation produces, including what its model calls cost, is
-- written to a live table.
CREATE TABLE simulation_agent_step (
    id bigserial PRIMARY KEY,
    simulation_id bigint NOT NULL REFERENCES simulation (id) ON DELETE CASCADE,
    step int NOT NULL CHECK (step >= 1),
    tool text NOT NULL,
    input jsonb NOT NULL DEFAULT '{}',
    output jsonb NOT NULL DEFAULT '{}',
    model text,
    input_tokens int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    cost_usd numeric(10, 6) NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (simulation_id, step)
);

-- A discarded simulation is final: nothing more is added to its trace. Steps are never edited.
CREATE FUNCTION simulation_agent_step_open() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    actual text;
BEGIN
    SELECT status INTO actual FROM simulation WHERE id = NEW.simulation_id FOR SHARE;
    IF actual IS NULL OR actual = 'discarded' THEN
        RAISE EXCEPTION 'simulation % is %', NEW.simulation_id, coalesce(actual, 'missing');
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER simulation_agent_step_open BEFORE INSERT ON simulation_agent_step
    FOR EACH ROW EXECUTE FUNCTION simulation_agent_step_open();
CREATE TRIGGER simulation_agent_step_immutable BEFORE UPDATE ON simulation_agent_step
    FOR EACH ROW EXECUTE FUNCTION forbid_update();

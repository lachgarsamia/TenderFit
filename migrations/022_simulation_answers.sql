-- Extend only Mode B's change vocabulary. Snapshot versioning lives in JSON; no live tables change.
ALTER TABLE simulation_change DROP CONSTRAINT IF EXISTS simulation_change_kind_check;
ALTER TABLE simulation_change ADD CONSTRAINT simulation_change_kind_check CHECK (kind IN (
    'set_condition_bound', 'set_condition_match', 'set_fact_validity', 'set_fact_evidence',
    'remove_fact', 'set_fact_value', 'add_fact', 'set_notice_field', 'set_requirement_text',
    'set_requirement_answer', 'set_override'
));

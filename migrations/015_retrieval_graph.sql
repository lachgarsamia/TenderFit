-- Typed links between passages of one notice version, resolved from the text itself.
CREATE TABLE passage_edge (
    id bigserial PRIMARY KEY,
    version_id bigint NOT NULL REFERENCES notice_version (id),
    from_passage_id bigint NOT NULL REFERENCES passage (id),
    to_passage_id bigint NOT NULL REFERENCES passage (id),
    kind text NOT NULL CHECK (kind IN ('REFERENCES', 'DEFINED_BY')),
    label text NOT NULL,
    UNIQUE (from_passage_id, to_passage_id, kind, label)
);
CREATE INDEX passage_edge_from ON passage_edge (from_passage_id);
CREATE INDEX passage_edge_version ON passage_edge (version_id);

-- What the verdict is made of, in plain structured form for the interface.
ALTER TABLE verdict
    ADD COLUMN required text,
    ADD COLUMN observed text,
    ADD COLUMN evidence_quality text CHECK (evidence_quality IN ('strong', 'weak', 'contradictory', 'missing', 'ambiguous')),
    ADD COLUMN uncertainty text,
    ADD COLUMN retrieval jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN reference_gaps text[] NOT NULL DEFAULT '{}';

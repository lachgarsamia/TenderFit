ALTER TABLE business_profile ADD COLUMN description text, ADD CONSTRAINT business_profile_name_key UNIQUE (name);

ALTER TABLE profile_fact
    ADD COLUMN label text,
    ADD COLUMN needs_update_reason text,
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX profile_fact_profile ON profile_fact (profile_id, attribute);

ALTER TABLE evidence_file
    ADD COLUMN filename text,
    ADD COLUMN storage_path text,
    ADD COLUMN page_count int,
    ADD COLUMN uploaded_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE fact_support ADD COLUMN verified boolean NOT NULL DEFAULT false, ADD COLUMN score real;

CREATE TABLE evidence_page (
    evidence_file_id bigint NOT NULL REFERENCES evidence_file (id),
    page int NOT NULL,
    text text NOT NULL,
    PRIMARY KEY (evidence_file_id, page)
);

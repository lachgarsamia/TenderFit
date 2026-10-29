CREATE TABLE raw_release (
    id bigserial PRIMARY KEY,
    ocid text NOT NULL,
    release_id text NOT NULL,
    release_date timestamptz NOT NULL,
    tags text[] NOT NULL,
    payload text NOT NULL,
    sha256 text NOT NULL,
    source_url text NOT NULL,
    fetched_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ocid, release_id)
);
CREATE INDEX raw_release_ocid ON raw_release (ocid);

CREATE TABLE buyer (
    id text PRIMARY KEY,
    name text NOT NULL,
    region text
);

CREATE TABLE opportunity (
    id bigserial PRIMARY KEY,
    ocid text NOT NULL UNIQUE,
    buyer_id text REFERENCES buyer (id),
    title text,
    legal_basis text,
    deadline timestamptz,
    cpv_codes text[] NOT NULL DEFAULT '{}',
    regions text[] NOT NULL DEFAULT '{}',
    status text NOT NULL DEFAULT 'unverified',
    status_reason text,
    last_checked_at timestamptz,
    current_version_id bigint
);
CREATE INDEX opportunity_cpv ON opportunity USING gin (cpv_codes);
CREATE INDEX opportunity_regions ON opportunity USING gin (regions);

CREATE TABLE notice_version (
    id bigserial PRIMARY KEY,
    opportunity_id bigint NOT NULL REFERENCES opportunity (id),
    ocid text NOT NULL,
    release_id text NOT NULL,
    raw_release_id bigint NOT NULL REFERENCES raw_release (id),
    notice_type text NOT NULL,
    release_tag text NOT NULL,
    published_at timestamptz NOT NULL,
    source_url text,
    retrieved_at timestamptz NOT NULL,
    content_hash text NOT NULL,
    fields jsonb NOT NULL,
    UNIQUE (ocid, release_id)
);
CREATE INDEX notice_version_buyer ON notice_version ((fields->>'buyer_id'));
ALTER TABLE opportunity ADD FOREIGN KEY (current_version_id) REFERENCES notice_version (id);

CREATE FUNCTION forbid_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END $$;
CREATE TRIGGER notice_version_immutable BEFORE UPDATE ON notice_version
    FOR EACH ROW EXECUTE FUNCTION forbid_update();
CREATE TRIGGER raw_release_immutable BEFORE UPDATE ON raw_release
    FOR EACH ROW EXECUTE FUNCTION forbid_update();

CREATE VIEW notice_version_numbered AS
SELECT nv.*, row_number() OVER (PARTITION BY ocid ORDER BY published_at, release_id) AS version_no
FROM notice_version nv;

CREATE TABLE lot (
    id bigserial PRIMARY KEY,
    opportunity_id bigint NOT NULL REFERENCES opportunity (id),
    lot_no text NOT NULL,
    title text,
    quantity numeric,
    status text NOT NULL DEFAULT 'unverified',
    UNIQUE (opportunity_id, lot_no)
);

CREATE TABLE linked_notice (
    id bigserial PRIMARY KEY,
    ocid text NOT NULL,
    release_id text NOT NULL,
    raw_release_id bigint NOT NULL REFERENCES raw_release (id),
    notice_type text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('award', 'cancellation')),
    lot_nos text[],
    published_at timestamptz NOT NULL,
    UNIQUE (ocid, release_id)
);
CREATE INDEX linked_notice_ocid ON linked_notice (ocid);

CREATE TABLE import_state (
    source text PRIMARY KEY,
    watermark timestamptz NOT NULL
);

CREATE TABLE import_run (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    window_from timestamptz NOT NULL,
    window_to timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    pages int NOT NULL DEFAULT 0,
    releases_seen int NOT NULL DEFAULT 0,
    releases_new int NOT NULL DEFAULT 0,
    throttled int NOT NULL DEFAULT 0,
    error text
);

CREATE TABLE job (
    id bigserial PRIMARY KEY,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    lock_key text,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 5,
    run_after timestamptz NOT NULL DEFAULT now(),
    locked_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX job_ready ON job (run_after) WHERE status = 'queued';

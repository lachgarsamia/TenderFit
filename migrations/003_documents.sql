ALTER TABLE document
    DROP CONSTRAINT document_access_check,
    ADD COLUMN filename text,
    ADD COLUMN content_type text,
    ADD COLUMN page_count int,
    ADD COLUMN note text,
    ADD COLUMN storage_path text,
    ADD COLUMN uploaded_by text,
    ADD CONSTRAINT document_access_check CHECK (access IN ('read', 'unreadable', 'not_retrieved'));

ALTER TABLE document_ref
    ADD COLUMN kind text NOT NULL DEFAULT 'document' CHECK (kind IN ('notice', 'document', 'referenced')),
    ADD COLUMN status text NOT NULL DEFAULT 'not_retrieved'
        CHECK (status IN ('read', 'unreadable', 'not_retrieved', 'confirmed_none')),
    ADD COLUMN note text,
    ADD COLUMN source_doc_id text;
CREATE INDEX document_ref_version ON document_ref (version_id);

ALTER TABLE passage
    ADD COLUMN ordinal int NOT NULL DEFAULT 0,
    ADD COLUMN kind text NOT NULL DEFAULT 'text' CHECK (kind IN ('text', 'table', 'field'));
CREATE INDEX passage_document ON passage (document_id, ordinal);
CREATE INDEX passage_sha ON passage (sha256);

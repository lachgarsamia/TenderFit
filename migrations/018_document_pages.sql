-- A PDF is read page by page. A partly scanned document is not "read": its readable pages are used, and the rest are named.
ALTER TABLE document
    ADD COLUMN page_status jsonb NOT NULL DEFAULT '[]',
    ADD COLUMN ocr_pages int[] NOT NULL DEFAULT '{}';

ALTER TABLE document_ref DROP CONSTRAINT document_ref_status_check,
    ADD CONSTRAINT document_ref_status_check CHECK (status IN ('read', 'partial', 'unreadable', 'not_retrieved', 'confirmed_none'));
ALTER TABLE document_ref_state DROP CONSTRAINT document_ref_state_status_check,
    ADD CONSTRAINT document_ref_state_status_check CHECK (status IN ('read', 'partial', 'unreadable', 'confirmed_none'));

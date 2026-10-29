ALTER TABLE document ADD COLUMN extracted_at timestamptz;
UPDATE document d SET extracted_at = now()
WHERE EXISTS (SELECT 1 FROM requirement r JOIN passage p ON p.id = r.passage_id WHERE p.document_id = d.id);

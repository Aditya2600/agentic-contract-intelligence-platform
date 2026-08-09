-- How a block's text was obtained, so a citation can be audited rather than trusted.
-- 'native_pdf' | 'gemma_vlm' | 'docx' | 'txt'. A reviewer looking at a quote needs to
-- know whether those characters came out of the file or out of a vision model reading a
-- rendered image: the second is evidence with a different failure mode.
ALTER TABLE document_blocks
    ADD COLUMN IF NOT EXISTS extraction_method TEXT NOT NULL DEFAULT 'txt';

-- `page` already exists on document_blocks; upload now actually fills it in.
CREATE INDEX IF NOT EXISTS document_blocks_method_idx
    ON document_blocks (document_id, extraction_method);

-- Extraction by document
-- Pages, scanned pages, rows read and the share accepted without review, per document.
SELECT doc_id,
       state,
       doc_type,
       effective,
       pages,
       ocr_pages,
       rows,
       accepted,
       review,
       ROUND(accepted / NULLIF(rows, 0), 3) AS accepted_share
FROM documents
ORDER BY state, effective, doc_id

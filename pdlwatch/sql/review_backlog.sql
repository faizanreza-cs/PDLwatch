-- Review backlog
-- Rows waiting for a person, by state and reason, with how many documents they come from.
SELECT state,
       review_reasons,
       COUNT(*) AS rows_to_review,
       COUNT(DISTINCT doc_id) AS documents
FROM review_queue
GROUP BY state, review_reasons
ORDER BY rows_to_review DESC, state

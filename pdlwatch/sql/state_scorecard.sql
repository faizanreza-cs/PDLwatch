-- State scorecard
-- Per state: the latest PDL's effective date, products listed, the share preferred, the share
-- under prior authorization or a quantity limit, and rows still waiting for review.
WITH latest AS (
    SELECT state, MAX(effective) AS effective
    FROM documents
    WHERE doc_type = 'pdl'
    GROUP BY state
),
review AS (
    SELECT state, COUNT(*) AS rows_in_review
    FROM review_queue
    GROUP BY state
)
SELECT a.state,
       a.state_name,
       l.effective,
       COUNT(*) AS products_listed,
       ROUND(COUNT_IF(a.status = 'preferred') / COUNT(*), 3) AS preferred_share,
       ROUND(COUNT_IF(a.pa) / COUNT(*), 3) AS pa_share,
       ROUND(COUNT_IF(a.ql) / COUNT(*), 3) AS ql_share,
       COALESCE(MAX(r.rows_in_review), 0) AS rows_in_review
FROM drug_access a
JOIN latest l ON l.state = a.state
LEFT JOIN review r ON r.state = a.state
GROUP BY a.state, a.state_name, l.effective
ORDER BY a.state

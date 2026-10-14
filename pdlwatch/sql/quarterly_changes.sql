-- Quarterly PDL changes
-- What changed between each state's last two PDL versions, by type, split into changes
-- confirmed automatically and changes waiting for review (usually a scanned version).
SELECT state,
       effective,
       change,
       COUNT(*) AS changes,
       CAST(COUNT_IF(NOT needs_review) AS INT) AS confirmed,
       CAST(COUNT_IF(needs_review) AS INT) AS to_review
FROM changes
GROUP BY state, effective, change
ORDER BY state, change

-- Status timeline
-- Every time a product's status changed in a state's PDL, with the version that changed it:
-- the history behind "when did we lose preferred status in Texas?". Confirmed rows only.
WITH confirmed AS (
    SELECT state, product, drug_class, status, valid_from, valid_to, doc_id, page,
           LAG(status) OVER (PARTITION BY state, product ORDER BY valid_from) AS previous_status
    FROM drug_access_history
    WHERE confirmed
)
SELECT state, product, drug_class, previous_status, status, valid_from, valid_to, doc_id, page
FROM confirmed
WHERE previous_status IS NULL OR previous_status <> status
ORDER BY product, state, valid_from

-- Class landscape across states
-- For every product: in how many states it is preferred, non-preferred (prior authorization) or
-- not covered, the share of listing states that prefer it, and how often PA or a quantity limit
-- applies. Uses each state's latest PDL and only rows accepted without review.
SELECT drug_class,
       product,
       ingredient,
       kind,
       COUNT(*) AS states_listing,
       CAST(COUNT_IF(status = 'preferred') AS INT) AS states_preferred,
       CAST(COUNT_IF(status = 'non_preferred') AS INT) AS states_non_preferred,
       CAST(COUNT_IF(status = 'not_covered') AS INT) AS states_not_covered,
       ROUND(COUNT_IF(status = 'preferred') / COUNT(*), 3) AS preferred_share,
       CAST(COUNT_IF(pa) AS INT) AS states_with_pa,
       CAST(COUNT_IF(ql) AS INT) AS states_with_ql
FROM drug_access
GROUP BY drug_class, product, ingredient, kind
ORDER BY drug_class, preferred_share DESC, product

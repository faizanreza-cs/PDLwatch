-- Brand preferred over generic
-- Ingredients where a state prefers the brand while its generic is non-preferred: usually a
-- supplemental rebate that makes the brand cheaper for Medicaid than the generic.
SELECT b.state,
       b.state_name,
       b.drug_class,
       b.ingredient,
       b.product AS preferred_brand,
       g.product AS non_preferred_generic,
       b.effective
FROM drug_access b
JOIN drug_access g
  ON g.state = b.state AND g.ingredient = b.ingredient
WHERE b.kind = 'brand' AND b.status = 'preferred'
  AND g.kind = 'generic' AND g.status = 'non_preferred'
ORDER BY b.drug_class, b.ingredient, b.state

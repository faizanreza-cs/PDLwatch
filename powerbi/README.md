# Power BI report kit

Everything needed to build a market access report in Power BI Desktop from the extracted PDLs:
where a drug is preferred, how a class is covered across states, what changed this quarter, and
what is waiting for review.

## 1. Export the data

```bash
pdlwatch build            # synthetic corpus (or `pdlwatch sync` for real state PDLs)
pdlwatch export-powerbi   # writes powerbi/data/*.csv
```

| Table | Grain | Use |
|---|---|---|
| `dim_state` | state | code and name (for the map) |
| `dim_product` | product | ingredient, brand or generic, therapeutic class |
| `dim_document` | document | state, type, effective date, pages, OCR pages, rows accepted and sent to review |
| `fact_access` | state x product | status, tier, PA, step therapy, quantity limit, brand over generic, source document and page (each state's latest PDL, rows accepted without review) |
| `fact_changes` | state x product x change | status, added, removed and quantity-limit changes between PDL versions |
| `fact_review` | extracted row | rows waiting for review, with confidence and reasons |
| `fact_extraction_quality` | group | accuracy by document type (synthetic corpus only, where a truth exists) |

## 2. Load and model

1. **Get data > Text/CSV** for each file. In Power Query, set `effective` to Date and `pa`, `st`,
   `ql`, `bvg`, `needs_review` to True/False.
2. **Relationships** (single direction, many-to-one):
   - `fact_access[state]` and `fact_changes[state]` → `dim_state[state]`
   - `fact_access[product]`, `fact_changes[product]` and `fact_review[product]` →
     `dim_product[product]`
   - `fact_review[doc_id]` → `dim_document[doc_id]`
3. **Measures:** paste [`measures.dax`](measures.dax), one block at a time.
4. **Theme:** View > Themes > Browse for themes > [`theme.json`](theme.json). Preferred is blue
   and non-preferred orange, as in the README charts.

## 3. Pages

| Page | Visuals |
|---|---|
| **Drug access** | Slicer: `dim_product[product]` (single select). Filled map: `dim_state[state_name]`, fill from `Status Colour` (Format > Fill colour > fx > Field value). Cards: `States Preferring`, `States Requiring PA`, `Preferred Share`. Table: state, `fact_access[status]`, `tier`, `ql_detail`, `doc_id`, `page`, so every status shows its source. |
| **Class landscape** | Slicer: `dim_product[drug_class]`. Stacked bar: `dim_product[product]` with `Preferred`, `Non-Preferred`, `Not Covered`. Matrix: products by state with `Status Label`, coloured by `Status Colour`. |
| **Quarterly changes** | Cards: `Status Changes`, `Lost Preferred`, `Gained Preferred`, `Changes Needing Review`. Table: `fact_changes` sorted by `needs_review`, then state. Slicer: `fact_changes[change]`. |
| **Review and quality** | Bar: `Rows in Review` by `fact_review[review_reasons]`. Table: `fact_review` sorted by `confidence` (the reviewer's work list). Cards: `Straight-through Rate`, `OCR Page Share`. Table: `fact_extraction_quality` (group, row recall, precision, accepted error rate). |

## 4. Screenshots

Save page screenshots to `docs/img/powerbi_*.png` and link them from the main README.

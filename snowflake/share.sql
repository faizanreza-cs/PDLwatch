-- Delivering PDLwatch to a customer's own Snowflake account (Secure Data Sharing).
--
-- The customer queries live data in their account: nothing is copied or exported, and each new
-- sync reaches them as soon as `pdlwatch load` finishes. A share exposes secure views only, so a
-- customer sees exactly the columns (and, with entitlements, the classes) their view selects,
-- and never the view's definition.
--
-- Run after snowflake/setup.sql and `pdlwatch load`, as a role that owns the PDLWATCH database
-- (ACCOUNTADMIN on a trial account). Sharing outside your Snowflake region needs a listing
-- (Snowflake Marketplace or a private listing) built on the same share.

USE ROLE ACCOUNTADMIN;
CREATE SCHEMA IF NOT EXISTS PDLWATCH.DELIVERY;

-- Which drug classes each customer account has bought. Leave it empty to give every account in
-- a share all classes; fill it to sell by therapeutic area.
CREATE TABLE IF NOT EXISTS PDLWATCH.DELIVERY.ENTITLEMENTS (
    ACCOUNT STRING,     -- the customer's account locator, as CURRENT_ACCOUNT() returns it
    DRUG_CLASS STRING   -- a class as written in DRUG_ACCESS.DRUG_CLASS, or '*' for all
);

-- Each state's status in force for each product, with the document and page it was read from.
-- Accepted and reviewer-confirmed rows only (the pipeline keeps rows in review out of it).
CREATE OR REPLACE SECURE VIEW PDLWATCH.DELIVERY.DRUG_ACCESS AS
SELECT a.STATE, a.STATE_NAME, a.PRODUCT, a.INGREDIENT, a.KIND, a.RXCUI, a.DRUG_CLASS, a.STATUS,
       a.TIER, a.PA, a.ST, a.QL, a.QL_DETAIL, a.BVG, a.EFFECTIVE, a.DOC_ID, a.PAGE,
       a.CONFIDENCE, a.REVIEWED
FROM PDLWATCH.MEDICAID.DRUG_ACCESS a
WHERE NOT EXISTS (SELECT 1 FROM PDLWATCH.DELIVERY.ENTITLEMENTS e
                  WHERE e.ACCOUNT = CURRENT_ACCOUNT())
   OR EXISTS (SELECT 1 FROM PDLWATCH.DELIVERY.ENTITLEMENTS e
              WHERE e.ACCOUNT = CURRENT_ACCOUNT() AND e.DRUG_CLASS IN (a.DRUG_CLASS, '*'));

-- Every change between consecutive versions of a state's list. NEEDS_REVIEW marks changes
-- that rest on a row still in review: show them, but don't act on them.
CREATE OR REPLACE SECURE VIEW PDLWATCH.DELIVERY.CHANGES AS
SELECT c.STATE, c.PRODUCT, c.DRUG_CLASS, c.CHANGE, c.BEFORE, c.AFTER, c.EFFECTIVE,
       c.FROM_DOC, c.TO_DOC, c.NEEDS_REVIEW
FROM PDLWATCH.MEDICAID.CHANGES c
WHERE NOT EXISTS (SELECT 1 FROM PDLWATCH.DELIVERY.ENTITLEMENTS e
                  WHERE e.ACCOUNT = CURRENT_ACCOUNT())
   OR EXISTS (SELECT 1 FROM PDLWATCH.DELIVERY.ENTITLEMENTS e
              WHERE e.ACCOUNT = CURRENT_ACCOUNT() AND e.DRUG_CLASS IN (c.DRUG_CLASS, '*'));

-- The status in force on any date: one interval per PDL version.
CREATE OR REPLACE SECURE VIEW PDLWATCH.DELIVERY.ACCESS_HISTORY AS
SELECT h.STATE, h.PRODUCT, h.DRUG_CLASS, h.STATUS, h.TIER, h.PA, h.ST, h.QL, h.QL_DETAIL,
       h.VALID_FROM, h.VALID_TO, h.CONFIRMED, h.DOC_ID, h.PAGE
FROM PDLWATCH.MEDICAID.DRUG_ACCESS_HISTORY h
WHERE NOT EXISTS (SELECT 1 FROM PDLWATCH.DELIVERY.ENTITLEMENTS e
                  WHERE e.ACCOUNT = CURRENT_ACCOUNT())
   OR EXISTS (SELECT 1 FROM PDLWATCH.DELIVERY.ENTITLEMENTS e
              WHERE e.ACCOUNT = CURRENT_ACCOUNT() AND e.DRUG_CLASS IN (h.DRUG_CLASS, '*'));

-- The source documents, so every status can be checked against its PDF.
CREATE OR REPLACE SECURE VIEW PDLWATCH.DELIVERY.DOCUMENTS AS
SELECT DOC_ID, STATE, STATE_NAME, TITLE, DOC_TYPE, EFFECTIVE, EFFECTIVE_FROM, FIRST_SEEN, PAGES,
       SOURCE
FROM PDLWATCH.MEDICAID.DOCUMENTS;

-- One share per customer. Replace ACME with the customer's name and <customer_account> with
-- their account identifier (in their Snowflake: Admin > Accounts, e.g. ORGNAME.ACCOUNTNAME).
CREATE SHARE IF NOT EXISTS PDLWATCH_ACME COMMENT = 'PDLwatch: Medicaid PDL status and changes';
GRANT USAGE ON DATABASE PDLWATCH TO SHARE PDLWATCH_ACME;
GRANT USAGE ON SCHEMA PDLWATCH.DELIVERY TO SHARE PDLWATCH_ACME;
GRANT SELECT ON VIEW PDLWATCH.DELIVERY.DRUG_ACCESS TO SHARE PDLWATCH_ACME;
GRANT SELECT ON VIEW PDLWATCH.DELIVERY.CHANGES TO SHARE PDLWATCH_ACME;
GRANT SELECT ON VIEW PDLWATCH.DELIVERY.ACCESS_HISTORY TO SHARE PDLWATCH_ACME;
GRANT SELECT ON VIEW PDLWATCH.DELIVERY.DOCUMENTS TO SHARE PDLWATCH_ACME;
-- ALTER SHARE PDLWATCH_ACME ADD ACCOUNTS = <customer_account>;

-- Sell by class: the customer's account sees only the classes listed for it.
-- INSERT INTO PDLWATCH.DELIVERY.ENTITLEMENTS VALUES ('<customer_account_locator>', 'GLP-1 Receptor Agonists');

-- In the customer's account:
--   CREATE DATABASE PDLWATCH FROM SHARE <your_org>.<your_account>.PDLWATCH_ACME;
--   SELECT STATE, STATUS, PA, QL, EFFECTIVE, DOC_ID, PAGE
--   FROM PDLWATCH.DELIVERY.DRUG_ACCESS WHERE PRODUCT = 'Ozempic' ORDER BY STATE;

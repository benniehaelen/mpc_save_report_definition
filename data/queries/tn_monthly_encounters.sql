-- Monthly distinct patient encounters for Tennessee hospitals, last ~24 months.
-- DuckDB translation of the BigQuery query in the original query.sql.
--
-- BigQuery -> DuckDB: SAFE_CAST -> TRY_CAST, FORMAT_DATE('%Y-%m', x) ->
-- strftime(x, '%Y-%m'), DATE_TRUNC(col, MONTH) -> date_trunc('month', col),
-- DATE_SUB(d, INTERVAL 24 MONTH) -> d - INTERVAL 24 MONTH, CONCAT -> concat.
--
-- CURRENT_DATE() is pinned to the POC anchor (2025-06-30) so the result is
-- deterministic against the seeded window. Swap it for `current_date` if you
-- regenerate the encounter data around the real today.
WITH
  tn_hospital_coids AS (
    SELECT DISTINCT coid
    FROM enterprise_ontology_gold.facility_master_site
    WHERE UPPER(state_code) = 'TN' AND UPPER(site_type) LIKE 'HOSPITAL%'
  ),
  encounter_base AS (
    SELECT
      CAST(TRY_CAST(e.admission_date_time AS TIMESTAMP) AS DATE) AS encounter_date,
      concat(e.coid, '|', e.patient_account_num) AS encounter_key
    FROM clinical_core_silver.encounter e
    WHERE
      e.latest_record_ind = 1
      AND e.patient_account_num IS NOT NULL
      AND TRY_CAST(e.admission_date_time AS TIMESTAMP) IS NOT NULL
      AND e.coid IN (SELECT coid FROM tn_hospital_coids)
  )
SELECT
  strftime(date_trunc('month', encounter_date), '%Y-%m') AS encounter_month,
  COUNT(DISTINCT encounter_key) AS total_encounters
FROM encounter_base
WHERE
  encounter_date
  BETWEEN date_trunc('month', DATE '2025-06-30' - INTERVAL 24 MONTH)
  AND DATE '2025-06-30'
GROUP BY encounter_month
ORDER BY encounter_month;

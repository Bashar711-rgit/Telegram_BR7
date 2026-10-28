-- ============================================================================
-- Telegram_BR7 — br7 schema v1 validation checklist
-- Run AFTER migrations/br7_schema_v1.sql has been applied.
-- Usage (psql):  psql "$STAGING_URL" -f migrations/validate_br7_v1.sql
-- Expected values are annotated next to each query.
-- This file is read-only: it never mutates data.
-- ============================================================================

\echo '=== 1. Table count (expect 44) ==='
SELECT count(*) AS br7_tables FROM information_schema.tables WHERE table_schema = 'br7';

\echo '=== 2. RLS not enabled (expect 0 rows) ==='
SELECT tablename FROM pg_tables WHERE schemaname = 'br7' AND rowsecurity = false;

\echo '=== 3. Unvalidated constraints (expect 0 rows) ==='
SELECT conname FROM pg_constraint c
JOIN pg_namespace n ON n.oid = c.connamespace
WHERE n.nspname = 'br7' AND NOT c.convalidated;

\echo '=== 4. claim_* functions (expect 3) ==='
SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'br7' AND proname LIKE 'claim_%';

\echo '=== 5. All 7 critical functions (expect 7) ==='
SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'br7' AND proname IN ('bump_app_stat','claim_processing_jobs',
  'claim_alert_deliveries','claim_background_jobs','enforce_retention',
  'sync_deleted_message','audit_trigger');

\echo '=== 6. Index count (expect >= 55) ==='
SELECT count(*) AS br7_indexes FROM pg_indexes WHERE schemaname = 'br7';

\echo '=== 7. Seeds loaded (expect 5) ==='
SELECT count(*) AS seeded_settings FROM br7.system_settings;

\echo '=== 8. Dashboard roles seeded (expect 3) ==='
SELECT count(*) AS seeded_roles FROM br7.dashboard_roles;

\echo '=== 9. Dashboard permissions seeded (expect 7) ==='
SELECT count(*) AS seeded_permissions FROM br7.dashboard_permissions;

\echo '=== 10. Gap-filler tables (expect 5) ==='
SELECT count(*) AS gap_fillers FROM information_schema.tables
WHERE table_schema = 'br7'
  AND table_name IN ('deleted_messages','app_stats','legacy_account_map',
                     'outbound_messages','message_media');

\echo '=== 11. Session integrity: no plaintext sessions (expect 0) ==='
SELECT count(*) AS bad_sessions FROM br7.telegram_account_sessions
WHERE status IN ('valid','configured')
  AND encrypted_session IS NULL AND secret_reference IS NULL;

\echo '=== 12. Migration registered (expect 1 row: 0001_br7_initial) ==='
SELECT version, description, applied_at, checksum
FROM public.schema_migrations ORDER BY applied_at;

\echo '=== 13. Updated_at triggers (expect 19) ==='
SELECT count(*) AS upd_triggers FROM information_schema.triggers
WHERE trigger_schema = 'br7' AND trigger_name LIKE '%_updated_at';

\echo '=== 14. Audit triggers (expect 3 distinct names) ==='
SELECT count(DISTINCT trigger_name) AS audit_triggers FROM information_schema.triggers
WHERE trigger_schema = 'br7' AND trigger_name LIKE 'trg_br7_audit_%';

\echo '=== 15. Schema grants: service_role has USAGE (expect 1) ==='
SELECT count(*) AS sr_usage FROM pg_namespace n
WHERE n.nspname = 'br7' AND has_schema_privilege('service_role', n.oid, 'USAGE');

\echo '=== VALIDATION COMPLETE ==='

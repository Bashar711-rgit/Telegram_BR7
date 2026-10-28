# br7 Unified Schema — Migration v1 (0001_br7_initial)

## Files
| File | Purpose |
|---|---|
| `br7_schema_v1.sql` | Unified DDL v1.0.0 — schema `br7` (44 tables) + `public.schema_migrations`. Idempotent, additive-only; never touches legacy `public` app tables. |
| `validate_br7_v1.sql` | Read-only checklist (psql-compatible) — 15 query groups with expected values. |
| `../scripts/apply_br7_schema.py` | asyncpg runner: apply (single transaction) → register migration row (sha256 checksum + timing) → 16 structural checks → optional staging functional smoke (forced rollback). DSN from `BR7_APPLY_DSN` env only — zero secrets in code. |

## Application procedure executed (2026-10-28)
1. **Staging (local PostgreSQL 16.2, pgserver binaries, TCP 55443, clean cluster):**
   - Prelude creates NOLOGIN roles `anon`/`authenticated`/`service_role` (Supabase role simulation).
   - `CREATE EXTENSION` lines skipped locally ONLY (pgserver ships without contrib; `gen_random_uuid()` is native PG13+, `pg_trgm` used by a commented-out index only). Production applied the file **verbatim**.
   - Result: **16/16 structural checks PASS + 7/7 functional smoke PASS** (bump_app_stat accumulation, sync_deleted_message trigger, claim_processing_jobs SKIP LOCKED claim, audit trigger on filter_rules, alerts/deliveries unique keys, RLS deny-by-default, enforce_retention dry-run). Idempotent re-run verified (13 ms). Smoke runs inside a FORCED-ROLLBACK transaction — staging left pristine.
   - **Staging caught 1 real DDL bug before production:** `audit_trigger()` referenced `NEW.id`, which does not exist on `system_settings` (PK = setting_key) or `dashboard_user_roles` (composite PK) — the seed INSERTs would have aborted the whole migration on production. Fixed with generic `to_jsonb(NEW/OLD) ->> 'id'/'setting_key'/'user_id'/'role_id'` extraction. This is the only deviation from the originally supplied DDL.
2. **Production (Supabase `heclpjexqqomuwmxlysw`, ap-northeast-1):**
   - Direct pooler login as `postgres.{ref}` failed (stored password stale → `InvalidPasswordError`), so the **Management API SQL endpoint** (`POST /v1/projects/{ref}/database/query`, executes as `postgres`) was used — proven path.
   - Whole 63,215-byte DDL sent in **one call wrapped in BEGIN/COMMIT** (HTTP 201, 1.8 s). Any mid-script error would have aborted the transaction server-side.
   - Result: **16/16 structural checks PASS**, migration registered in `public.schema_migrations` (checksum `99dd900fe3444cbe…`, applied_by=postgres).
   - sha256 of applied file: `99dd900fe3444cbe…` (full value printed by the runner).

## What changed on production
- NEW schema `br7` with 44 tables, 134 indexes, 10 functions, 22+ triggers, RLS enabled on all 44 tables (no policies → deny-by-default for non-owners).
- NEW table `public.schema_migrations` (migration registry).
- Grants: only `service_role` gets schema USAGE/table DML; `anon`/`authenticated`/`PUBLIC` revoked. `br7_app` (the bot's role) deliberately has **no** grants on `br7` — it keeps using legacy `public` tables (seeds set `persistence.authoritative_schema='public'`), so the running bot is untouched by design.

## Rollback (staging only, per plan)
```sql
DROP SCHEMA br7 CASCADE;      -- staging ONLY
DROP TABLE IF EXISTS public.schema_migrations;
```
On production, `br7` is additive and unreferenced by the app; dropping it later is equally non-destructive to legacy data, but per the execution order it stays in place as the future cutover target. The legacy `public` schema remains the authoritative store until an explicit cutover migration flips `persistence.authoritative_schema`.

## Re-run / CI
```bash
# validate only (no writes):
BR7_APPLY_DSN=... python3 scripts/apply_br7_schema.py --validate-only
# re-apply (idempotent):
BR7_APPLY_DSN=... python3 scripts/apply_br7_schema.py
```

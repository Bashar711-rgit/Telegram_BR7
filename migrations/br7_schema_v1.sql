-- ============================================================================
-- Telegram_BR7 — Unified Database DDL
-- ============================================================================
-- Version:        1.0.0
-- Target:         Supabase PostgreSQL / PostgreSQL 14+
-- Scope:          Schema `br7` (target) + public.schema_migrations
-- Mode:           Idempotent — safe to re-run
-- Prerequisites:  - Backup verified
--                 - Staging mirror of production
--                 - Review against live database.py before applying
-- ============================================================================
-- ⚠️ WARNING
-- 1) Run via migration tool (Alembic / Flyway / Sqitch / dbmate). Do NOT
--    run manually in production.
-- 2) Verify all CHECK constraints match real application data first.
-- 3) Never store plaintext Telegram sessions, bot tokens, or DB URLs.
-- 4) service_role bypasses RLS. Never expose it to browser/mobile clients.
-- 5) Some tables include gap-filler (H*): deleted_messages, app_stats,
--    legacy_account_map, outbound_messages (optional), message_media (optional).
-- ============================================================================


-- ============================================================================
-- SECTION 0 — Preflight: Extensions, Schema, Migrations Table
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- pg_stat_statements requires shared_preload_libraries on the server side.
-- Enable at the instance level, not here.
-- CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS br7;
COMMENT ON SCHEMA br7 IS
'Telegram_BR7 operational schema. Source of truth after cutover. Access via trusted backend only.';

-- Migration tracking table (in public, as recommended)
CREATE TABLE IF NOT EXISTS public.schema_migrations (
  version         text PRIMARY KEY,
  description     text NOT NULL,
  applied_at      timestamptz NOT NULL DEFAULT now(),
  applied_by      text NOT NULL DEFAULT current_user,
  checksum        text NOT NULL,
  execution_ms    integer,
  rollback_script text
);
COMMENT ON TABLE public.schema_migrations IS
'Centralized migration log. Populated by the migration tool only.';


-- ============================================================================
-- SECTION 1 — Helper Functions
-- ============================================================================

CREATE OR REPLACE FUNCTION br7.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
  NEW.updated_at := clock_timestamp();
  RETURN NEW;
END;
$fn$;
COMMENT ON FUNCTION br7.set_updated_at() IS
'Trigger helper. Sets updated_at to now() on every UPDATE.';


-- ============================================================================
-- SECTION A — Telegram Accounts, Sessions, Health
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.telegram_accounts (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_key           text NOT NULL UNIQUE,
  display_name          text NOT NULL DEFAULT '',
  phone_masked          text,
  status                text NOT NULL DEFAULT 'disabled'
                        CHECK (status IN ('disabled','disconnected','connecting','connected',
                          'monitoring','warning','flood_wait','reconnecting','error','failed')),
  enabled               boolean NOT NULL DEFAULT false,
  session_configured    boolean NOT NULL DEFAULT false,
  last_connected_at     timestamptz,
  last_seen_at          timestamptz,
  last_error_code       text,
  last_error_at         timestamptz,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (length(account_key) BETWEEN 1 AND 100)
);
COMMENT ON TABLE br7.telegram_accounts IS
'Operating Telegram accounts (bots or user sessions) that perform monitoring. NOT the same as monitored users.';

CREATE INDEX IF NOT EXISTS ix_br7_accounts_status_enabled
  ON br7.telegram_accounts (status, enabled);
CREATE INDEX IF NOT EXISTS ix_br7_accounts_last_seen
  ON br7.telegram_accounts (last_seen_at DESC NULLS LAST);


CREATE TABLE IF NOT EXISTS br7.telegram_account_sessions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id            uuid NOT NULL REFERENCES br7.telegram_accounts(id) ON DELETE RESTRICT,
  session_kind          text NOT NULL DEFAULT 'string'
                        CHECK (session_kind IN ('string','qr','file','external_secret')),
  secret_reference      text,
  encrypted_session     bytea,
  encryption_key_id     text,
  status                text NOT NULL DEFAULT 'configured'
                        CHECK (status IN ('configured','valid','expired','revoked','error')),
  validated_at          timestamptz,
  expires_at            timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id),
  CHECK (encrypted_session IS NULL OR encryption_key_id IS NOT NULL),
  CHECK (encrypted_session IS NULL OR secret_reference IS NULL)
);
COMMENT ON TABLE br7.telegram_account_sessions IS
'Encrypted or externally referenced session material ONLY. Never store plaintext sessions.';

-- Additional session integrity check (added via DO block for idempotency)
DO $ck$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_br7_sessions_valid_has_secret'
      AND conrelid = 'br7.telegram_account_sessions'::regclass
  ) THEN
    ALTER TABLE br7.telegram_account_sessions
      ADD CONSTRAINT chk_br7_sessions_valid_has_secret
      CHECK (status <> 'valid'
             OR secret_reference IS NOT NULL
             OR encrypted_session IS NOT NULL);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_br7_sessions_active_has_secret'
      AND conrelid = 'br7.telegram_account_sessions'::regclass
  ) THEN
    ALTER TABLE br7.telegram_account_sessions
      ADD CONSTRAINT chk_br7_sessions_active_has_secret
      CHECK (status IN ('expired','revoked','error')
             OR encrypted_session IS NOT NULL
             OR secret_reference IS NOT NULL);
  END IF;
END
$ck$;


CREATE TABLE IF NOT EXISTS br7.telegram_account_health (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  account_id            uuid NOT NULL REFERENCES br7.telegram_accounts(id) ON DELETE RESTRICT,
  status                text NOT NULL CHECK (status IN ('disconnected','connecting','connected',
                          'monitoring','warning','flood_wait','reconnecting','error','failed')),
  checked_at            timestamptz NOT NULL DEFAULT now(),
  latency_ms            integer CHECK (latency_ms IS NULL OR latency_ms >= 0),
  error_code            text,
  details               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(details) = 'object')
);
CREATE INDEX IF NOT EXISTS ix_br7_account_health_time
  ON br7.telegram_account_health (account_id, checked_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_account_health_status
  ON br7.telegram_account_health (status, checked_at DESC);


-- ============================================================================
-- SECTION B — Sources, Users, Access
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.telegram_sources (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_chat_id      bigint NOT NULL UNIQUE,
  source_type           text NOT NULL CHECK (source_type IN ('group','supergroup','channel','user','unknown')),
  title                 text NOT NULL DEFAULT '',
  username              text,
  enabled               boolean NOT NULL DEFAULT true,
  status                text NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','paused','unavailable','restricted','archived')),
  last_seen_at          timestamptz,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_sources_enabled_status
  ON br7.telegram_sources (enabled, status);
CREATE INDEX IF NOT EXISTS ix_br7_sources_username
  ON br7.telegram_sources (username) WHERE username IS NOT NULL;


CREATE TABLE IF NOT EXISTS br7.telegram_account_sources (
  account_id            uuid NOT NULL REFERENCES br7.telegram_accounts(id) ON DELETE RESTRICT,
  source_id             uuid NOT NULL REFERENCES br7.telegram_sources(id) ON DELETE RESTRICT,
  enabled               boolean NOT NULL DEFAULT true,
  access_status         text NOT NULL DEFAULT 'unknown'
                        CHECK (access_status IN ('unknown','accessible','restricted','not_member','private','error')),
  last_checked_at       timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, source_id)
);
CREATE INDEX IF NOT EXISTS ix_br7_account_sources_source
  ON br7.telegram_account_sources (source_id, enabled);


CREATE TABLE IF NOT EXISTS br7.telegram_users (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  telegram_user_id      bigint NOT NULL UNIQUE,
  username              text,
  display_name          text,
  is_bot                boolean NOT NULL DEFAULT false,
  first_seen_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_at          timestamptz,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_users_username
  ON br7.telegram_users (username) WHERE username IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_br7_users_last_seen
  ON br7.telegram_users (last_seen_at DESC NULLS LAST);


CREATE TABLE IF NOT EXISTS br7.telegram_usernames_history (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  user_id               uuid NOT NULL REFERENCES br7.telegram_users(id) ON DELETE RESTRICT,
  username              text,
  valid_from            timestamptz NOT NULL DEFAULT now(),
  valid_to              timestamptz,
  CHECK (valid_to IS NULL OR valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS ix_br7_username_history
  ON br7.telegram_usernames_history (user_id, valid_from DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_br7_username_current_per_user
  ON br7.telegram_usernames_history (user_id) WHERE valid_to IS NULL;


CREATE TABLE IF NOT EXISTS br7.sender_access (
  account_id            uuid NOT NULL REFERENCES br7.telegram_accounts(id) ON DELETE RESTRICT,
  user_id               uuid NOT NULL REFERENCES br7.telegram_users(id) ON DELETE RESTRICT,
  access_hash           bigint,
  access_hash_encrypted bytea,
  encryption_key_id     text,
  observed_at           timestamptz NOT NULL DEFAULT now(),
  expires_at            timestamptz,
  PRIMARY KEY (account_id, user_id),
  CHECK (access_hash IS NULL OR access_hash_encrypted IS NULL),
  CHECK (access_hash_encrypted IS NULL OR encryption_key_id IS NOT NULL)
);
COMMENT ON TABLE br7.sender_access IS
'Access context scoped to (operating account × sender). Prefer encrypted storage.';

-- Enforce: at least one hash form must be present
DO $ck$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_br7_sender_access_has_hash'
      AND conrelid = 'br7.sender_access'::regclass
  ) THEN
    ALTER TABLE br7.sender_access
      ADD CONSTRAINT chk_br7_sender_access_has_hash
      CHECK (access_hash IS NOT NULL OR access_hash_encrypted IS NOT NULL);
  END IF;
END
$ck$;

CREATE INDEX IF NOT EXISTS ix_br7_sender_access_user
  ON br7.sender_access (user_id);


-- ============================================================================
-- SECTION C — Messages, Analysis, Processing
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.messages (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id             uuid NOT NULL REFERENCES br7.telegram_sources(id) ON DELETE RESTRICT,
  sender_id             uuid REFERENCES br7.telegram_users(id) ON DELETE SET NULL,
  telegram_message_id   bigint NOT NULL,
  grouped_id            bigint,
  message_date          timestamptz NOT NULL,
  edited_at             timestamptz,
  text_content          text,
  content_fingerprint   text NOT NULL,
  content_type          text NOT NULL DEFAULT 'text'
                        CHECK (content_type IN ('text','photo','video','document','audio','sticker','service','other')),
  reply_to_message_id   bigint,
  is_forwarded          boolean NOT NULL DEFAULT false,
  is_deleted            boolean NOT NULL DEFAULT false,
  received_at           timestamptz NOT NULL DEFAULT now(),
  expires_at            timestamptz,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  UNIQUE (source_id, telegram_message_id)
);
COMMENT ON TABLE br7.messages IS
'Canonical ingested messages. Define retention policy for text_content and metadata.';

-- Gap-filler G3: source_url (added idempotently for existing deployments)
ALTER TABLE br7.messages ADD COLUMN IF NOT EXISTS source_url text;

CREATE INDEX IF NOT EXISTS ix_br7_messages_source_date
  ON br7.messages (source_id, message_date DESC);
CREATE INDEX IF NOT EXISTS ix_br7_messages_sender_date
  ON br7.messages (sender_id, message_date DESC) WHERE sender_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_br7_messages_date
  ON br7.messages (message_date DESC);
CREATE INDEX IF NOT EXISTS ix_br7_messages_fingerprint
  ON br7.messages (content_fingerprint, message_date DESC);
CREATE INDEX IF NOT EXISTS ix_br7_messages_expiry
  ON br7.messages (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_br7_messages_source_url
  ON br7.messages (source_url) WHERE source_url IS NOT NULL;
-- BRIN for large time-series queries (cheap, effective for append-only)
CREATE INDEX IF NOT EXISTS brin_br7_messages_date
  ON br7.messages USING brin (message_date);
-- Trigram index for text search (optional; enable when measured beneficial)
-- CREATE INDEX gin_br7_messages_text_trgm
--   ON br7.messages USING gin (text_content gin_trgm_ops)
--   WHERE text_content IS NOT NULL;


CREATE TABLE IF NOT EXISTS br7.message_analysis (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid NOT NULL REFERENCES br7.messages(id) ON DELETE RESTRICT,
  engine_version        text NOT NULL,
  intent_key            text,
  help_score            double precision CHECK (help_score IS NULL OR help_score BETWEEN 0 AND 1),
  advertisement_score   double precision CHECK (advertisement_score IS NULL OR advertisement_score BETWEEN 0 AND 1),
  spam_score            double precision CHECK (spam_score IS NULL OR spam_score BETWEEN 0 AND 1),
  confidence            double precision CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  signals               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(signals) = 'object'),
  analyzed_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (message_id, engine_version)
);
CREATE INDEX IF NOT EXISTS ix_br7_analysis_intent_time
  ON br7.message_analysis (intent_key, analyzed_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_analysis_confidence
  ON br7.message_analysis (confidence DESC NULLS LAST);


CREATE TABLE IF NOT EXISTS br7.filter_decisions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid NOT NULL REFERENCES br7.messages(id) ON DELETE RESTRICT,
  analysis_id           uuid REFERENCES br7.message_analysis(id) ON DELETE RESTRICT,
  decision              text NOT NULL CHECK (decision IN ('accept','review','reject','ignore')),
  decision_reason       text NOT NULL DEFAULT '',
  rule_version          text,
  engine_version        text NOT NULL DEFAULT 'unknown',
  matched_rules         jsonb NOT NULL DEFAULT '[]'::jsonb
                        CHECK (jsonb_typeof(matched_rules) = 'array'),
  decided_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_decisions_message_time
  ON br7.filter_decisions (message_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_decisions_outcome_time
  ON br7.filter_decisions (decision, decided_at DESC);


CREATE TABLE IF NOT EXISTS br7.message_context (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid NOT NULL REFERENCES br7.messages(id) ON DELETE RESTRICT,
  context_type          text NOT NULL,
  context_data          jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(context_data) = 'object'),
  expires_at            timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_message_context_expiry
  ON br7.message_context (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_br7_message_context_message
  ON br7.message_context (message_id);


CREATE TABLE IF NOT EXISTS br7.message_deduplication (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  scope_key             text NOT NULL,
  fingerprint           text NOT NULL,
  first_message_id      uuid REFERENCES br7.messages(id) ON DELETE SET NULL,
  first_seen_at         timestamptz NOT NULL DEFAULT now(),
  expires_at            timestamptz NOT NULL,
  UNIQUE (scope_key, fingerprint)
);
CREATE INDEX IF NOT EXISTS ix_br7_dedup_expiry
  ON br7.message_deduplication (expires_at);


CREATE TABLE IF NOT EXISTS br7.message_processing_jobs (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid NOT NULL REFERENCES br7.messages(id) ON DELETE RESTRICT,
  job_type              text NOT NULL,
  status                text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','retrying','completed','failed','cancelled')),
  attempt_count         integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts          integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
  available_at          timestamptz NOT NULL DEFAULT now(),
  locked_at             timestamptz,
  locked_by             text,
  last_error_code       text,
  completed_at          timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (attempt_count <= max_attempts)
);
CREATE INDEX IF NOT EXISTS ix_br7_processing_jobs_ready
  ON br7.message_processing_jobs (available_at, created_at)
  WHERE status IN ('pending','retrying');
CREATE INDEX IF NOT EXISTS ix_br7_processing_jobs_message
  ON br7.message_processing_jobs (message_id, created_at DESC);


-- ============================================================================
-- SECTION H1 — Gap-filler: deleted_messages (was G1)
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.deleted_messages (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid REFERENCES br7.messages(id) ON DELETE SET NULL,
  source_id             uuid NOT NULL REFERENCES br7.telegram_sources(id) ON DELETE RESTRICT,
  telegram_message_id   bigint NOT NULL,
  sender_id             uuid REFERENCES br7.telegram_users(id) ON DELETE SET NULL,
  text_snapshot         text,
  content_hash          text,
  original_date         timestamptz,
  detected_deleted_at   timestamptz NOT NULL DEFAULT now(),
  detected_by_account   uuid REFERENCES br7.telegram_accounts(id) ON DELETE SET NULL,
  recovered             boolean NOT NULL DEFAULT false,
  recovered_at          timestamptz,
  recovery_attempts     smallint NOT NULL DEFAULT 0 CHECK (recovery_attempts BETWEEN 0 AND 20),
  last_recovery_error   text,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (recovered = false OR recovered_at IS NOT NULL)
);
COMMENT ON TABLE br7.deleted_messages IS
'Full deletion & recovery history. Restored from legacy public.deleted_messages semantics.';

CREATE UNIQUE INDEX IF NOT EXISTS ux_br7_deleted_source_message
  ON br7.deleted_messages (source_id, telegram_message_id);
CREATE INDEX IF NOT EXISTS ix_br7_deleted_detected
  ON br7.deleted_messages (detected_deleted_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_deleted_pending_recovery
  ON br7.deleted_messages (detected_deleted_at) WHERE recovered = false;


-- ============================================================================
-- SECTION D — Rules, Intents, Feedback, Evaluation
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.intent_definitions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  intent_key            text NOT NULL UNIQUE,
  display_name          text NOT NULL,
  description           text,
  enabled               boolean NOT NULL DEFAULT true,
  configuration         jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(configuration) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE IF NOT EXISTS br7.filter_rules (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_key              text NOT NULL UNIQUE,
  rule_type             text NOT NULL CHECK (rule_type IN ('allow','deny','boost','penalty','ad','spam','negation','context')),
  title                 text NOT NULL,
  enabled               boolean NOT NULL DEFAULT true,
  priority              integer NOT NULL DEFAULT 100,
  weight                double precision NOT NULL DEFAULT 0
                        CHECK (weight BETWEEN -100 AND 100),
  configuration         jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(configuration) = 'object'),
  created_by            text,
  updated_by            text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_rules_active_priority
  ON br7.filter_rules (priority, rule_type) WHERE enabled = true;


CREATE TABLE IF NOT EXISTS br7.filter_keywords (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  rule_id               uuid NOT NULL REFERENCES br7.filter_rules(id) ON DELETE RESTRICT,
  phrase                text NOT NULL,
  normalized_phrase     text NOT NULL,
  locale                text NOT NULL DEFAULT 'ar',
  dialect               text,
  match_type            text NOT NULL DEFAULT 'substring'
                        CHECK (match_type IN ('exact','substring','token','regex','phrase')),
  weight                double precision NOT NULL DEFAULT 0
                        CHECK (weight BETWEEN -100 AND 100),
  enabled               boolean NOT NULL DEFAULT true,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (rule_id, normalized_phrase, match_type, locale)
);
CREATE INDEX IF NOT EXISTS ix_br7_keywords_active_locale
  ON br7.filter_keywords (locale, rule_id) WHERE enabled = true;
CREATE INDEX IF NOT EXISTS ix_br7_keywords_normalized
  ON br7.filter_keywords (normalized_phrase);


CREATE TABLE IF NOT EXISTS br7.intent_patterns (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  intent_id             uuid NOT NULL REFERENCES br7.intent_definitions(id) ON DELETE RESTRICT,
  pattern               text NOT NULL,
  normalized_pattern    text NOT NULL,
  pattern_type          text NOT NULL DEFAULT 'phrase'
                        CHECK (pattern_type IN ('phrase','regex','token_sequence')),
  weight                double precision NOT NULL DEFAULT 0
                        CHECK (weight BETWEEN -100 AND 100),
  enabled               boolean NOT NULL DEFAULT true,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (intent_id, normalized_pattern, pattern_type)
);
CREATE INDEX IF NOT EXISTS ix_br7_intent_patterns_active
  ON br7.intent_patterns (intent_id) WHERE enabled = true;


CREATE TABLE IF NOT EXISTS br7.filter_rule_versions (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  rule_id               uuid NOT NULL REFERENCES br7.filter_rules(id) ON DELETE RESTRICT,
  version_no            integer NOT NULL CHECK (version_no > 0),
  snapshot              jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
  changed_by            text NOT NULL DEFAULT 'system',
  change_reason         text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (rule_id, version_no)
);
CREATE INDEX IF NOT EXISTS ix_br7_rule_versions_time
  ON br7.filter_rule_versions (rule_id, created_at DESC);


CREATE TABLE IF NOT EXISTS br7.filter_feedback (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid REFERENCES br7.messages(id) ON DELETE RESTRICT,
  alert_id              uuid,
  feedback_type         text NOT NULL CHECK (feedback_type IN (
                          'true_positive','false_positive','true_negative','false_negative','needs_review')),
  reviewer_subject      uuid,
  reviewer_label        text NOT NULL DEFAULT 'unknown',
  notes                 text,
  decision_snapshot     jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(decision_snapshot) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (message_id IS NOT NULL OR alert_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_br7_feedback_time_type
  ON br7.filter_feedback (created_at DESC, feedback_type);
CREATE INDEX IF NOT EXISTS ix_br7_feedback_message
  ON br7.filter_feedback (message_id, created_at DESC) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_br7_feedback_alert
  ON br7.filter_feedback (alert_id) WHERE alert_id IS NOT NULL;


CREATE TABLE IF NOT EXISTS br7.filter_evaluation_runs (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  engine_version        text NOT NULL,
  dataset_name          text NOT NULL,
  dataset_version       text,
  total_examples        integer NOT NULL DEFAULT 0 CHECK (total_examples >= 0),
  true_positive         integer NOT NULL DEFAULT 0 CHECK (true_positive >= 0),
  false_positive        integer NOT NULL DEFAULT 0 CHECK (false_positive >= 0),
  true_negative         integer NOT NULL DEFAULT 0 CHECK (true_negative >= 0),
  false_negative        integer NOT NULL DEFAULT 0 CHECK (false_negative >= 0),
  metrics               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metrics) = 'object'),
  started_at            timestamptz NOT NULL DEFAULT now(),
  completed_at          timestamptz,
  CHECK (true_positive + false_positive + true_negative + false_negative <= total_examples)
);
CREATE INDEX IF NOT EXISTS ix_br7_eval_runs_time
  ON br7.filter_evaluation_runs (started_at DESC);


-- ============================================================================
-- SECTION E — Alerts, Deliveries, Actions, DLQ
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.alert_destinations (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  destination_key       text NOT NULL UNIQUE,
  destination_type      text NOT NULL CHECK (destination_type IN ('telegram_chat','telegram_channel','webhook','internal')),
  telegram_chat_id      bigint,
  display_name          text NOT NULL DEFAULT '',
  enabled               boolean NOT NULL DEFAULT true,
  configuration         jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(configuration) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (destination_type NOT IN ('telegram_chat','telegram_channel') OR telegram_chat_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_br7_destinations_enabled
  ON br7.alert_destinations (enabled, destination_type);


CREATE TABLE IF NOT EXISTS br7.alerts (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid NOT NULL REFERENCES br7.messages(id) ON DELETE RESTRICT,
  decision_id           uuid REFERENCES br7.filter_decisions(id) ON DELETE RESTRICT,
  status                text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','queued','delivering','sent','partially_sent','failed','dismissed')),
  priority              smallint NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  deduplication_key     text NOT NULL UNIQUE,
  title                 text,
  rendered_text         text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  expires_at            timestamptz
);
CREATE INDEX IF NOT EXISTS ix_br7_alerts_status_time
  ON br7.alerts (status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_alerts_message
  ON br7.alerts (message_id);
CREATE INDEX IF NOT EXISTS ix_br7_alerts_priority_time
  ON br7.alerts (priority, created_at DESC) WHERE status IN ('pending','queued','delivering');


-- Deferred FK: filter_feedback.alert_id → alerts.id (created after both tables exist)
DO $fk$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'fk_br7_filter_feedback_alert'
      AND conrelid = 'br7.filter_feedback'::regclass
  ) THEN
    ALTER TABLE br7.filter_feedback
      ADD CONSTRAINT fk_br7_filter_feedback_alert
      FOREIGN KEY (alert_id) REFERENCES br7.alerts(id) ON DELETE RESTRICT;
  END IF;
END
$fk$;


CREATE TABLE IF NOT EXISTS br7.alert_deliveries (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alert_id              uuid NOT NULL REFERENCES br7.alerts(id) ON DELETE RESTRICT,
  destination_id        uuid NOT NULL REFERENCES br7.alert_destinations(id) ON DELETE RESTRICT,
  status                text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','sent','retrying','failed','cancelled')),
  attempt_count         integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts          integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
  next_attempt_at       timestamptz NOT NULL DEFAULT now(),
  idempotency_key       text NOT NULL UNIQUE,
  external_message_id   text,
  last_error_code       text,
  last_error_at         timestamptz,
  sent_at               timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (alert_id, destination_id),
  CHECK (attempt_count <= max_attempts)
);
COMMENT ON TABLE br7.alert_deliveries IS
'Persistent delivery state with idempotency keys. Use bounded retries and transactional claims.';

CREATE INDEX IF NOT EXISTS ix_br7_deliveries_due
  ON br7.alert_deliveries (next_attempt_at, id)
  WHERE status IN ('pending','retrying');
CREATE INDEX IF NOT EXISTS ix_br7_deliveries_alert_time
  ON br7.alert_deliveries (alert_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_deliveries_status_time
  ON br7.alert_deliveries (status, created_at DESC);


CREATE TABLE IF NOT EXISTS br7.alert_actions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  alert_id              uuid NOT NULL REFERENCES br7.alerts(id) ON DELETE RESTRICT,
  action_type           text NOT NULL CHECK (action_type IN ('accept','reject','false_positive','false_negative','open_source','contact_sender','requeue','dismiss','other')),
  actor_subject         uuid,
  actor_label           text NOT NULL DEFAULT 'system',
  notes                 text,
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_alert_actions_alert_time
  ON br7.alert_actions (alert_id, created_at DESC);


CREATE TABLE IF NOT EXISTS br7.dead_letter_queue (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id                uuid REFERENCES br7.message_processing_jobs(id) ON DELETE SET NULL,
  delivery_id           uuid REFERENCES br7.alert_deliveries(id) ON DELETE SET NULL,
  item_type             text NOT NULL CHECK (item_type IN ('processing_job','alert_delivery','other')),
  reason_code           text NOT NULL,
  payload_reference     text,
  attempts              integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  status                text NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','investigating','requeued','resolved','discarded')),
  created_at            timestamptz NOT NULL DEFAULT now(),
  resolved_at           timestamptz,
  CHECK (job_id IS NOT NULL OR delivery_id IS NOT NULL OR payload_reference IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_br7_dlq_open_time
  ON br7.dead_letter_queue (status, created_at DESC);

-- Semantic integrity: item_type must match which ID is populated
DO $ck$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'chk_br7_dlq_item_matches'
      AND conrelid = 'br7.dead_letter_queue'::regclass
  ) THEN
    ALTER TABLE br7.dead_letter_queue
      ADD CONSTRAINT chk_br7_dlq_item_matches
      CHECK (
        (item_type = 'processing_job' AND job_id IS NOT NULL AND delivery_id IS NULL)
        OR (item_type = 'alert_delivery' AND delivery_id IS NOT NULL AND job_id IS NULL)
        OR (item_type = 'other')
      );
  END IF;
END
$ck$;


-- ============================================================================
-- SECTION F — Dashboard, Auth, Settings, Audit
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.dashboard_users (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  auth_subject          uuid UNIQUE,
  email_label           text,
  display_name          text NOT NULL DEFAULT '',
  enabled               boolean NOT NULL DEFAULT true,
  last_login_at         timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE br7.dashboard_users IS
'Application profile only. Credentials belong in Supabase Auth or external IdP.';


CREATE TABLE IF NOT EXISTS br7.dashboard_roles (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role_key              text NOT NULL UNIQUE,
  description           text,
  created_at            timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE IF NOT EXISTS br7.dashboard_permissions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  permission_key        text NOT NULL UNIQUE,
  description           text,
  created_at            timestamptz NOT NULL DEFAULT now()
);


CREATE TABLE IF NOT EXISTS br7.dashboard_user_roles (
  user_id               uuid NOT NULL REFERENCES br7.dashboard_users(id) ON DELETE RESTRICT,
  role_id               uuid NOT NULL REFERENCES br7.dashboard_roles(id) ON DELETE RESTRICT,
  assigned_by           uuid REFERENCES br7.dashboard_users(id) ON DELETE SET NULL,
  assigned_at           timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, role_id)
);
CREATE INDEX IF NOT EXISTS ix_br7_dashboard_user_roles_role
  ON br7.dashboard_user_roles (role_id);


CREATE TABLE IF NOT EXISTS br7.dashboard_role_permissions (
  role_id               uuid NOT NULL REFERENCES br7.dashboard_roles(id) ON DELETE RESTRICT,
  permission_id         uuid NOT NULL REFERENCES br7.dashboard_permissions(id) ON DELETE RESTRICT,
  PRIMARY KEY (role_id, permission_id)
);
CREATE INDEX IF NOT EXISTS ix_br7_dashboard_role_permissions_perm
  ON br7.dashboard_role_permissions (permission_id);


CREATE TABLE IF NOT EXISTS br7.system_settings (
  setting_key           text PRIMARY KEY,
  value_json            jsonb NOT NULL,
  value_type            text NOT NULL DEFAULT 'json'
                        CHECK (value_type IN ('string','integer','number','boolean','json')),
  description           text,
  is_secret_reference   boolean NOT NULL DEFAULT false,
  updated_by            uuid REFERENCES br7.dashboard_users(id) ON DELETE SET NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE br7.system_settings IS
'Operational settings. Store references for secrets, never secret values.';


CREATE TABLE IF NOT EXISTS br7.audit_logs (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  actor_user_id         uuid REFERENCES br7.dashboard_users(id) ON DELETE SET NULL,
  actor_label           text NOT NULL DEFAULT 'system',
  action                text NOT NULL,
  entity_type           text NOT NULL,
  entity_id             text,
  outcome               text NOT NULL DEFAULT 'success'
                        CHECK (outcome IN ('success','failure','denied')),
  correlation_id        text,
  safe_details          jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(safe_details) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE br7.audit_logs IS
'Administrative audit trail. Never place credentials, session strings, or message bodies in safe_details.';

CREATE INDEX IF NOT EXISTS ix_br7_audit_time
  ON br7.audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_audit_entity
  ON br7.audit_logs (entity_type, entity_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_audit_actor
  ON br7.audit_logs (actor_user_id, created_at DESC) WHERE actor_user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS brin_br7_audit_time
  ON br7.audit_logs USING brin (created_at);


-- ============================================================================
-- SECTION G — Operations, Events, Errors, Metrics
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.system_events (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  event_type            text NOT NULL,
  severity              text NOT NULL DEFAULT 'info'
                        CHECK (severity IN ('debug','info','warning','error','critical')),
  component             text NOT NULL,
  account_id            uuid REFERENCES br7.telegram_accounts(id) ON DELETE SET NULL,
  correlation_id        text,
  summary               text NOT NULL,
  details               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(details) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_events_time
  ON br7.system_events (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_events_severity_time
  ON br7.system_events (severity, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_br7_events_component_time
  ON br7.system_events (component, created_at DESC);
CREATE INDEX IF NOT EXISTS brin_br7_events_time
  ON br7.system_events USING brin (created_at);


CREATE TABLE IF NOT EXISTS br7.error_logs (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  error_code            text,
  severity              text NOT NULL DEFAULT 'error'
                        CHECK (severity IN ('warning','error','critical')),
  component             text NOT NULL,
  operation             text,
  correlation_id        text,
  safe_message          text NOT NULL,
  stack_fingerprint     text,
  details               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(details) = 'object'),
  occurred_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_errors_time_severity
  ON br7.error_logs (occurred_at DESC, severity);
CREATE INDEX IF NOT EXISTS ix_br7_errors_fingerprint
  ON br7.error_logs (stack_fingerprint, occurred_at DESC) WHERE stack_fingerprint IS NOT NULL;
CREATE INDEX IF NOT EXISTS brin_br7_errors_time
  ON br7.error_logs USING brin (occurred_at);


CREATE TABLE IF NOT EXISTS br7.background_jobs (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_type              text NOT NULL,
  job_key               text UNIQUE,
  status                text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','running','retrying','completed','failed','cancelled')),
  payload               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(payload) = 'object'),
  attempt_count         integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts          integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
  available_at          timestamptz NOT NULL DEFAULT now(),
  locked_at             timestamptz,
  locked_by             text,
  last_error_code       text,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  completed_at          timestamptz,
  CHECK (attempt_count <= max_attempts)
);
CREATE INDEX IF NOT EXISTS ix_br7_background_jobs_ready
  ON br7.background_jobs (available_at, created_at)
  WHERE status IN ('pending','retrying');


CREATE TABLE IF NOT EXISTS br7.system_health_snapshots (
  id                    bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  overall_status        text NOT NULL CHECK (overall_status IN ('healthy','degraded','unhealthy')),
  database_status       text NOT NULL CHECK (database_status IN ('healthy','degraded','unhealthy','unknown')),
  telegram_status       text NOT NULL CHECK (telegram_status IN ('healthy','degraded','unhealthy','unknown')),
  queue_depth           bigint CHECK (queue_depth IS NULL OR queue_depth >= 0),
  memory_bytes          bigint CHECK (memory_bytes IS NULL OR memory_bytes >= 0),
  cpu_percent           double precision CHECK (cpu_percent IS NULL OR cpu_percent BETWEEN 0 AND 100),
  details               jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(details) = 'object'),
  captured_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_health_snapshots_time
  ON br7.system_health_snapshots (captured_at DESC);


CREATE TABLE IF NOT EXISTS br7.hourly_metrics (
  metric_hour           timestamptz NOT NULL,
  metric_name           text NOT NULL,
  dimensions            jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(dimensions) = 'object'),
  dimensions_hash       text NOT NULL,
  metric_value          double precision NOT NULL,
  sample_count          bigint NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (metric_hour, metric_name, dimensions_hash)
);
CREATE INDEX IF NOT EXISTS ix_br7_hourly_metrics_name_time
  ON br7.hourly_metrics (metric_name, metric_hour DESC);


CREATE TABLE IF NOT EXISTS br7.daily_metrics (
  metric_date           date NOT NULL,
  metric_name           text NOT NULL,
  dimensions            jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(dimensions) = 'object'),
  dimensions_hash       text NOT NULL,
  metric_value          double precision NOT NULL,
  sample_count          bigint NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (metric_date, metric_name, dimensions_hash)
);
CREATE INDEX IF NOT EXISTS ix_br7_daily_metrics_name_date
  ON br7.daily_metrics (metric_name, metric_date DESC);


-- ============================================================================
-- SECTION H2 — Gap-filler: app_stats (was G2)
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.app_stats (
  stat_key     text PRIMARY KEY,
  stat_value   bigint NOT NULL DEFAULT 0 CHECK (stat_value >= 0),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE br7.app_stats IS
'Global key/value counters. Replaces legacy public.app_stats. Update via br7.bump_app_stat only.';


-- ============================================================================
-- SECTION H3 — Gap-filler: legacy_account_map (was G4)
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.legacy_account_map (
  legacy_owner_account  text NOT NULL,
  br7_account_id        uuid NOT NULL REFERENCES br7.telegram_accounts(id) ON DELETE RESTRICT,
  mapping_reason        text NOT NULL DEFAULT 'migration',
  mapped_at             timestamptz NOT NULL DEFAULT now(),
  verified              boolean NOT NULL DEFAULT false,
  PRIMARY KEY (legacy_owner_account, br7_account_id)
);
COMMENT ON TABLE br7.legacy_account_map IS
'Bridges legacy owner_account semantics to br7 UUID accounts. Drop after Legacy decommission.';

CREATE UNIQUE INDEX IF NOT EXISTS ux_br7_legacy_map_owner
  ON br7.legacy_account_map (legacy_owner_account) WHERE verified = true;


-- ============================================================================
-- SECTION H4 — Optional: outbound_messages (P2)
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.outbound_messages (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id            uuid NOT NULL REFERENCES br7.telegram_accounts(id) ON DELETE RESTRICT,
  destination_chat_id   bigint NOT NULL,
  related_message_id    uuid REFERENCES br7.messages(id) ON DELETE SET NULL,
  action_type           text NOT NULL CHECK (action_type IN ('reply','forward','contact_sender','other')),
  status                text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','sent','failed','retrying','cancelled')),
  attempt_count         integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts          integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
  external_message_id   bigint,
  last_error_code       text,
  sent_at               timestamptz,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CHECK (attempt_count <= max_attempts)
);
CREATE INDEX IF NOT EXISTS ix_br7_outbound_status_time
  ON br7.outbound_messages (status, created_at DESC);


-- ============================================================================
-- SECTION H5 — Optional: message_media (P2)
-- ============================================================================

CREATE TABLE IF NOT EXISTS br7.message_media (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  message_id            uuid NOT NULL REFERENCES br7.messages(id) ON DELETE CASCADE,
  media_type            text NOT NULL
                        CHECK (media_type IN ('photo','video','document','audio','sticker','voice','other')),
  mime_type             text,
  file_size_bytes       bigint CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
  file_id               text,
  storage_path          text,
  content_hash          text,
  width                 integer CHECK (width IS NULL OR width > 0),
  height                integer CHECK (height IS NULL OR height > 0),
  duration_seconds      integer CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
  metadata              jsonb NOT NULL DEFAULT '{}'::jsonb
                        CHECK (jsonb_typeof(metadata) = 'object'),
  created_at            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_br7_message_media_message
  ON br7.message_media (message_id);
CREATE INDEX IF NOT EXISTS ix_br7_message_media_hash
  ON br7.message_media (content_hash) WHERE content_hash IS NOT NULL;


-- ============================================================================
-- SECTION I — Triggers (updated_at, sync_deleted, audit)
-- ============================================================================

-- I.1 updated_at triggers (idempotent — drop and recreate)
DO $block$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'telegram_accounts','telegram_account_sessions','telegram_sources',
    'telegram_account_sources','telegram_users','message_processing_jobs',
    'intent_definitions','filter_rules','filter_keywords','alert_destinations',
    'alerts','alert_deliveries','dashboard_users','system_settings','background_jobs',
    'deleted_messages','hourly_metrics','daily_metrics','outbound_messages'
  ]
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_%I_updated_at ON br7.%I', t, t);
    EXECUTE format(
      'CREATE TRIGGER trg_%I_updated_at BEFORE UPDATE ON br7.%I '
      'FOR EACH ROW EXECUTE FUNCTION br7.set_updated_at()', t, t);
  END LOOP;
END
$block$;


-- I.2 Sync deleted_messages from messages.is_deleted transitions
CREATE OR REPLACE FUNCTION br7.sync_deleted_message()
RETURNS trigger
LANGUAGE plpgsql
AS $fn$
BEGIN
  IF NEW.is_deleted = true AND (OLD.is_deleted IS DISTINCT FROM true) THEN
    INSERT INTO br7.deleted_messages (
      message_id, source_id, telegram_message_id, sender_id,
      text_snapshot, content_hash, original_date, detected_deleted_at
    ) VALUES (
      NEW.id, NEW.source_id, NEW.telegram_message_id, NEW.sender_id,
      NEW.text_content, NEW.content_fingerprint, NEW.message_date, now()
    )
    ON CONFLICT (source_id, telegram_message_id) DO UPDATE
      SET detected_deleted_at = LEAST(br7.deleted_messages.detected_deleted_at, now()),
          updated_at = now();
  END IF;
  RETURN NEW;
END;
$fn$;

DROP TRIGGER IF EXISTS trg_br7_messages_sync_deleted ON br7.messages;
CREATE TRIGGER trg_br7_messages_sync_deleted
AFTER UPDATE OF is_deleted ON br7.messages
FOR EACH ROW
WHEN (OLD.is_deleted IS DISTINCT FROM NEW.is_deleted)
EXECUTE FUNCTION br7.sync_deleted_message();


-- I.3 Audit trigger for sensitive tables
-- Generic entity-id extraction: uses to_jsonb() so it works on tables whose
-- primary key is not `id` (system_settings.setting_key, composite
-- dashboard_user_roles) — a direct NEW.id reference would fail on those.
CREATE OR REPLACE FUNCTION br7.audit_trigger()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = br7, pg_temp
AS $fn$
DECLARE
  v_actor     text := current_setting('app.actor_label', true);
  v_row       jsonb;
  v_entity_id text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    v_row := to_jsonb(OLD);
  ELSE
    v_row := to_jsonb(NEW);
  END IF;
  v_entity_id := COALESCE(v_row ->> 'id',
                          v_row ->> 'setting_key',
                          v_row ->> 'user_id',
                          v_row ->> 'role_id');
  INSERT INTO br7.audit_logs (
    actor_label, action, entity_type, entity_id, outcome, safe_details
  ) VALUES (
    COALESCE(v_actor, current_user),
    TG_OP,
    TG_TABLE_NAME,
    v_entity_id,
    'success',
    jsonb_build_object('schema', TG_TABLE_SCHEMA)
  );
  RETURN COALESCE(NEW, OLD);
END;
$fn$;

DROP TRIGGER IF EXISTS trg_br7_audit_filter_rules ON br7.filter_rules;
CREATE TRIGGER trg_br7_audit_filter_rules
AFTER INSERT OR UPDATE OR DELETE ON br7.filter_rules
FOR EACH ROW EXECUTE FUNCTION br7.audit_trigger();

DROP TRIGGER IF EXISTS trg_br7_audit_system_settings ON br7.system_settings;
CREATE TRIGGER trg_br7_audit_system_settings
AFTER INSERT OR UPDATE OR DELETE ON br7.system_settings
FOR EACH ROW EXECUTE FUNCTION br7.audit_trigger();

DROP TRIGGER IF EXISTS trg_br7_audit_user_roles ON br7.dashboard_user_roles;
CREATE TRIGGER trg_br7_audit_user_roles
AFTER INSERT OR UPDATE OR DELETE ON br7.dashboard_user_roles
FOR EACH ROW EXECUTE FUNCTION br7.audit_trigger();


-- ============================================================================
-- SECTION J — Atomic Functions (SECURITY DEFINER with safe search_path)
-- ============================================================================

-- J.1 Counter increment
CREATE OR REPLACE FUNCTION br7.bump_app_stat(p_key text, p_delta bigint)
RETURNS bigint
LANGUAGE sql
SECURITY DEFINER
SET search_path = br7, pg_temp
AS $$
  INSERT INTO br7.app_stats (stat_key, stat_value)
  VALUES (p_key, p_delta)
  ON CONFLICT (stat_key) DO UPDATE
    SET stat_value = br7.app_stats.stat_value + EXCLUDED.stat_value,
        updated_at = now()
  RETURNING stat_value;
$$;
REVOKE ALL ON FUNCTION br7.bump_app_stat(text, bigint) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION br7.bump_app_stat(text, bigint) TO service_role;


-- J.2 Claim processing jobs
CREATE OR REPLACE FUNCTION br7.claim_processing_jobs(p_limit int, p_worker text)
RETURNS SETOF br7.message_processing_jobs
LANGUAGE sql
SECURITY DEFINER
SET search_path = br7, pg_temp
AS $$
  UPDATE br7.message_processing_jobs j
  SET status = 'processing',
      attempt_count = attempt_count + 1,
      locked_at = clock_timestamp(),
      locked_by = p_worker,
      updated_at = clock_timestamp()
  WHERE j.id IN (
    SELECT id FROM br7.message_processing_jobs
    WHERE status IN ('pending','retrying')
      AND available_at <= now()
    ORDER BY available_at, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  )
  RETURNING j.*;
$$;
REVOKE ALL ON FUNCTION br7.claim_processing_jobs(int, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION br7.claim_processing_jobs(int, text) TO service_role;


-- J.3 Claim alert deliveries
CREATE OR REPLACE FUNCTION br7.claim_alert_deliveries(p_limit int, p_worker text)
RETURNS SETOF br7.alert_deliveries
LANGUAGE sql
SECURITY DEFINER
SET search_path = br7, pg_temp
AS $$
  UPDATE br7.alert_deliveries d
  SET status = 'processing',
      attempt_count = attempt_count + 1,
      updated_at = clock_timestamp()
  WHERE d.id IN (
    SELECT id FROM br7.alert_deliveries
    WHERE status IN ('pending','retrying')
      AND next_attempt_at <= now()
    ORDER BY next_attempt_at, id
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  )
  RETURNING d.*;
$$;
REVOKE ALL ON FUNCTION br7.claim_alert_deliveries(int, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION br7.claim_alert_deliveries(int, text) TO service_role;


-- J.4 Claim background jobs
CREATE OR REPLACE FUNCTION br7.claim_background_jobs(p_limit int, p_worker text)
RETURNS SETOF br7.background_jobs
LANGUAGE sql
SECURITY DEFINER
SET search_path = br7, pg_temp
AS $$
  UPDATE br7.background_jobs j
  SET status = 'running',
      attempt_count = attempt_count + 1,
      locked_at = clock_timestamp(),
      locked_by = p_worker,
      updated_at = clock_timestamp()
  WHERE j.id IN (
    SELECT id FROM br7.background_jobs
    WHERE status IN ('pending','retrying')
      AND available_at <= now()
    ORDER BY available_at, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT p_limit
  )
  RETURNING j.*;
$$;
REVOKE ALL ON FUNCTION br7.claim_background_jobs(int, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION br7.claim_background_jobs(int, text) TO service_role;


-- J.5 Retention enforcement (dry-run by default)
CREATE OR REPLACE FUNCTION br7.enforce_retention(p_dry_run boolean DEFAULT true)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = br7, pg_temp
AS $fn$
DECLARE
  v_msgs_days int := COALESCE(
    (SELECT (value_json #>> '{}')::int FROM br7.system_settings
     WHERE setting_key = 'retention.messages_days'), 90);
  v_events_days int := COALESCE(
    (SELECT (value_json #>> '{}')::int FROM br7.system_settings
     WHERE setting_key = 'retention.events_days'), 180);
  v_msgs int := 0;
  v_events int := 0;
BEGIN
  IF p_dry_run THEN
    SELECT count(*) INTO v_msgs FROM br7.messages
    WHERE received_at < now() - (v_msgs_days || ' days')::interval
      AND text_content IS NOT NULL;
    SELECT count(*) INTO v_events FROM br7.system_events
    WHERE created_at < now() - (v_events_days || ' days')::interval;
  ELSE
    UPDATE br7.messages
    SET text_content = NULL,
        metadata = metadata || jsonb_build_object('redacted_at', now())
    WHERE received_at < now() - (v_msgs_days || ' days')::interval
      AND text_content IS NOT NULL;
    GET DIAGNOSTICS v_msgs = ROW_COUNT;

    DELETE FROM br7.system_events
    WHERE created_at < now() - (v_events_days || ' days')::interval;
    GET DIAGNOSTICS v_events = ROW_COUNT;
  END IF;

  RETURN jsonb_build_object(
    'dry_run', p_dry_run,
    'messages_affected', v_msgs,
    'events_affected', v_events,
    'messages_days', v_msgs_days,
    'events_days', v_events_days
  );
END;
$fn$;
REVOKE ALL ON FUNCTION br7.enforce_retention(boolean) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION br7.enforce_retention(boolean) TO service_role;


-- ============================================================================
-- SECTION K — Row Level Security
-- ============================================================================
-- Enabled on ALL br7 tables. No policies for anon/authenticated by default.
-- Access is intentionally restricted to service_role.
-- Add narrowly-scoped policies only when browser access is explicitly designed.

DO $rls$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'telegram_accounts','telegram_account_sessions','telegram_account_health',
    'telegram_sources','telegram_account_sources','telegram_users',
    'telegram_usernames_history','sender_access','messages','message_analysis',
    'filter_decisions','message_context','message_deduplication',
    'message_processing_jobs','deleted_messages',
    'intent_definitions','filter_rules','filter_keywords','intent_patterns',
    'filter_rule_versions','filter_feedback','filter_evaluation_runs',
    'alert_destinations','alerts','alert_deliveries','alert_actions','dead_letter_queue',
    'dashboard_users','dashboard_roles','dashboard_permissions','dashboard_user_roles',
    'dashboard_role_permissions','system_settings','audit_logs','system_events',
    'error_logs','background_jobs','system_health_snapshots','hourly_metrics','daily_metrics',
    'app_stats','legacy_account_map','outbound_messages','message_media'
  ]
  LOOP
    EXECUTE format('ALTER TABLE br7.%I ENABLE ROW LEVEL SECURITY', t);
  END LOOP;
END
$rls$;


-- ============================================================================
-- SECTION L — Grants (Supabase roles assumed; adapt if outside Supabase)
-- ============================================================================

REVOKE ALL ON SCHEMA br7 FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA br7 FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL TABLES IN SCHEMA br7 FROM PUBLIC, anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA br7 FROM PUBLIC, anon, authenticated;

GRANT USAGE ON SCHEMA br7 TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA br7 TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA br7 TO service_role;
GRANT EXECUTE ON FUNCTION br7.set_updated_at() TO service_role;

ALTER DEFAULT PRIVILEGES IN SCHEMA br7
  REVOKE ALL ON TABLES FROM PUBLIC, anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA br7
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES IN SCHEMA br7
  GRANT USAGE, SELECT ON SEQUENCES TO service_role;


-- ============================================================================
-- SECTION M — Seed Data (idempotent)
-- ============================================================================

INSERT INTO br7.system_settings (setting_key, value_json, value_type, description)
VALUES
  ('persistence.authoritative_schema', '"public"'::jsonb, 'string',
   'public=legacy primary, br7=new primary, dual=both'),
  ('persistence.dual_write_enabled',   'false'::jsonb,    'boolean',
   'Mirror writes to both schemas temporarily'),
  ('persistence.read_source',          '"public"'::jsonb, 'string',
   'Which schema serves reads: public | br7'),
  ('retention.messages_days',          '90'::jsonb,       'integer',
   'Retention window for message text_content'),
  ('retention.events_days',            '180'::jsonb,      'integer',
   'Retention window for operational events')
ON CONFLICT (setting_key) DO NOTHING;


INSERT INTO br7.dashboard_roles (role_key, description)
VALUES
  ('admin',    'Full administrative access'),
  ('operator', 'Daily operations and monitoring'),
  ('viewer',   'Read-only access')
ON CONFLICT (role_key) DO NOTHING;


INSERT INTO br7.dashboard_permissions (permission_key, description)
VALUES
  ('rules.read',    'View filter rules'),
  ('rules.write',   'Create/edit filter rules'),
  ('alerts.read',   'View alerts'),
  ('alerts.manage', 'Manage alert actions'),
  ('settings.read', 'View system settings'),
  ('settings.write','Modify system settings'),
  ('audit.read',    'View audit logs')
ON CONFLICT (permission_key) DO NOTHING;


-- ============================================================================
-- SECTION N — Post-apply Validation (run separately, not here)
-- ============================================================================
-- Copy these into your CI validation job:
--
-- -- All br7 tables exist
-- SELECT count(*) FROM information_schema.tables WHERE table_schema = 'br7';
-- -- Expect ~44
--
-- -- RLS enabled everywhere
-- SELECT tablename FROM pg_tables
-- WHERE schemaname = 'br7' AND rowsecurity = false;
-- -- Expect 0 rows
--
-- -- All FKs resolved
-- SELECT conname FROM pg_constraint c
-- JOIN pg_namespace n ON n.oid = c.connamespace
-- WHERE n.nspname = 'br7' AND NOT c.convalidated;
-- -- Expect 0 rows
--
-- -- Required functions exist
-- SELECT proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
-- WHERE n.nspname = 'br7' AND proname LIKE 'claim_%';
-- -- Expect 3 rows
--
-- -- Indexes present
-- SELECT count(*) FROM pg_indexes WHERE schemaname = 'br7';
-- -- Expect 55+
--
-- -- Seeds loaded
-- SELECT setting_key FROM br7.system_settings;
-- -- Expect 5 rows
--
-- -- Session integrity: no plaintext sessions
-- SELECT count(*) FROM br7.telegram_account_sessions
-- WHERE status IN ('valid','configured')
--   AND encrypted_session IS NULL AND secret_reference IS NULL;
-- -- Expect 0


-- ============================================================================
-- END OF DDL
-- ============================================================================

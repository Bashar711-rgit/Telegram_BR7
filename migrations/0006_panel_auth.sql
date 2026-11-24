-- ═══════════════════════════════════════════════════════════════════════
-- 0006_panel_auth.sql — المرحلة 1: مستخدمو اللوحة والجلسات الدائمة
-- additive بحت: لا حذف ولا إعادة تسمية ولا تعديل أعمدة موجودة.
-- يُطبَّق تلقائياً عبر database.py:_migrate_panel_auth عند الإقلاع —
-- هذا الملف مرجع توثيقي/يدوي (نفس نمط 0003 وما قبله).
-- متوافق مع المحركين: SQLite وPostgreSQL (عبر ترجمة _pg الموجودة).
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS panel_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    password_algo TEXT NOT NULL DEFAULT 'argon2id',
    display_name TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'viewer',
    is_active INTEGER DEFAULT 1,
    must_change_password INTEGER DEFAULT 0,
    totp_enabled INTEGER DEFAULT 0,
    totp_secret_encrypted TEXT DEFAULT '',
    recovery_codes TEXT DEFAULT '[]',
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0,
    last_login_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS panel_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf_hash TEXT NOT NULL DEFAULT '',
    user_id INTEGER NOT NULL,
    username TEXT DEFAULT '',
    role TEXT DEFAULT 'viewer',
    must_change INTEGER DEFAULT 0,
    ip TEXT DEFAULT '',
    user_agent TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    last_seen REAL DEFAULT 0,
    expires_at REAL DEFAULT 0,
    revoked_at REAL
);

-- إثراء سجل التدقيق (تُطبق كـALTER TABLE ADD COLUMN في _migrate_panel_auth
-- مع فحص الوجود للمحركين — SQLite لا يدعم IF NOT EXISTS للاعمدة):
--   audit_logs.ip          TEXT DEFAULT ''
--   audit_logs.user_agent  TEXT DEFAULT ''
--   audit_logs.request_id  TEXT DEFAULT ''

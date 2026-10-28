"""v11.8.2 — اختبارات انحدار موجة P2/P3 (F-11/F-12/F-18/F-21/F-22/F-23/
F-25/F-26/F-27/F-28/F-30) + حواجز أمان مخطط br7.

المنهجية:
  * سلوكية حيثما أمكن (F-18 عبر SQLite حقيقي، F-26/F-27/F-22 بمناهج
    مباشرة) لضمان أن الإصلاح يعمل فعلاً لا مجرد وجوده.
  * حواجز مصدر (source pins) للكود الميت/العقود المزالة كي لا تعود
    الانحلالات القديمة بصمت (نفس أسلوب test_intent_fixes السابقة).
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402,F401


# ═══════════════════════ F-12: الكتلة المكررة للنتيجة المجهولة ═══════════════════════

def test_f12_policy_matrix_covers_removed_block():
    """سياسة unknown_send_result_policy وحدها تغطي كل مسارات الكتلة القديمة."""
    from monitors import unknown_send_result_policy as pol
    assert pol("other_error", True) == "fallback"
    assert pol("timeout_attempt_1", True) == "verify"
    assert pol("network_error_x", True) == "verify"
    assert pol("timeout_attempt_1", False) == "stop"
    assert pol("network_error_x", False) == "fallback"


def test_f12_duplicated_block_removed():
    src = (PROJECT_DIR / "monitors.py").read_text()
    # الشرط المزدوج القديم (_unk_reason.startswitch) لا يعود موجوداً
    assert '_unk_reason.startswith("timeout_attempt") or _unk_reason.startswith("network_error")' not in src


# ═══════════════════════ F-11: توحيد عقد /health ═══════════════════════

def test_f11_fallback_health_contract_matches_dashboard():
    msrc = (PROJECT_DIR / "main.py").read_text()
    # نفس مفاتيح العقد الأدنى في dashboard.py /health
    assert '"database": "down" if db_down else "ok"' in msrc
    assert '"db_healthy": db_healthy' in msrc
    # نفس دلالة 503
    assert "status=503 if db_down else 200" in msrc
    # التفاصيل انتقلت إلى /health/full (نفس مسار اللوحة)
    assert msrc.count("add_get('/health/full'") == 1
    # /health لم يعد يفضي السنابشوت التشغيلي
    health_block = msrc.split("async def health_handler", 1)[1].split("async def health_full_handler", 1)[0]
    assert "fast_capture" not in health_block and "antispam" not in health_block


# ═══════════════════════ F-21: تعريف واحد لـ_pick_alertui_account ═══════════════════════

def test_f21_single_pick_alertui_definition():
    src = (PROJECT_DIR / "main.py").read_text()
    assert src.count("def _pick_alertui_account(") == 1


# ═══════════════════════ F-22: عداد طول مخصص + مفاتيح الإحصاء الكامنة ═══════════════════════

@pytest.mark.asyncio
async def test_f22_dedicated_length_counter_increments():
    from monitors import EnhancedAccountMonitor
    m = object.__new__(EnhancedAccountMonitor)
    m._stats = {"length_rejected": 0, "source_skipped": 0, "spam_skipped": 0}
    m._stats_lock = asyncio.Lock()
    await m._inc_stat("length_rejected")
    await m._inc_stat("source_skipped")
    await m._inc_stat("spam_skipped")
    assert m._stats["length_rejected"] == 1
    assert m._stats["source_skipped"] == 1
    assert m._stats["spam_skipped"] == 1


def test_f22_stats_dict_contains_counter_keys():
    """الإصلاح الكامن: source_skipped/spam_skipped كانت تُهمَل (مفاتيح غائبة)."""
    src = (PROJECT_DIR / "monitors.py").read_text()
    assert '"length_rejected": 0' in src
    assert '"source_skipped": 0' in src
    assert '"spam_skipped": 0' in src
    # بوابة الطول تستخدم العداد المخصص لا errors
    assert '_inc_stat("length_rejected")' in src


# ═══════════════════════ F-23: HealthMonitor عبر _ping ═══════════════════════

def test_f23_healthmonitor_uses_ping():
    src = (PROJECT_DIR / "monitors.py").read_text()
    assert '_fetchone("SELECT 1 AS ping")' not in src
    assert "await self._bot.db._ping()" in src


# ═══════════════════════ F-25: المسار الاحتياطي لا يعيد الأزرار المرفوضة ═══════════════════════

def test_f25_fallback_uses_kb_gate():
    src = (PROJECT_DIR / "monitors.py").read_text()
    assert "buttons=buttons" not in src
    assert src.count("buttons=_kb_for_this_send()") >= 2


# ═══════════════════════ F-26: بوابة محادثة زر copy_ ═══════════════════════

class _FakeCopyEvent:
    def __init__(self, chat_id, data: bytes = b"copy_abcd1234"):
        self.chat_id = chat_id
        self.data = data
        self.answers: list = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))


class _FakeCopyDB:
    def __init__(self, text="SECRET-ALERT-TEXT"):
        self.text = text
        self.calls = 0

    async def get_alert_text_by_hash(self, h):
        self.calls += 1
        return self.text

    async def get_message_text_by_hash(self, h):
        self.calls += 1
        return self.text


def test_f26_same_chat_matrix():
    from alert_ui import AlertBotClient
    f = AlertBotClient._same_chat
    assert f(-1001234567890, 1234567890)          # -100… مقابل الداخلية
    assert f(-1001234567890, -1001234567890)      # نفس الصيغة
    assert not f(-1001234567890, None)
    assert not f(None, 1234567890)
    assert not f("abc", 1234567890)
    assert not f(-1001234567890, 999)             # محادثة أخرى = رفض


@pytest.mark.asyncio
async def test_f26_copy_denies_foreign_chat_before_db():
    from alert_ui import AlertBotClient
    db = _FakeCopyDB()
    c = AlertBotClient(api_id=1, api_hash="h", token="",
                       db=db, target_chat_id=-1001234567890)
    # محادثة غريبة → رفض عام + صفر استعلام DB
    bad = _FakeCopyEvent(-1009998887776)
    await c._handle_copy_callback(bad)
    assert bad.answers and bad.answers[0][0] == "غير متاح هنا"
    assert db.calls == 0
    # مجموعة الهدف بالصيغة الداخلية → النص الحقيقي
    good = _FakeCopyEvent(1234567890)
    await c._handle_copy_callback(good)
    assert good.answers and good.answers[0][0] == "SECRET-ALERT-TEXT"
    assert db.calls == 1


# ═══════════════════════ F-27: توحيد نمط اسم المستخدم ═══════════════════════

def test_f27_username_regex_unified():
    import message_context
    import nav_resolver
    import sender_resolver

    assert nav_resolver._USERNAME_RE.pattern == "^[A-Za-z][A-Za-z0-9_]{3,31}$"
    assert message_context._USERNAME_RE.pattern == "^[A-Za-z][A-Za-z0-9_]{3,31}$"
    # sender_resolver يجمع بكسل — أجبره على إعادة التجميع بالنمط الجديد
    sender_resolver._USERNAME_RE = None
    parsed = sender_resolver.parse_telegram_link("@valid_user1")
    assert parsed.kind == "username"
    assert sender_resolver._USERNAME_RE.pattern == "^[A-Za-z][A-Za-z0-9_]{3,31}$"
    # 33+ حرفاً ليس مستخدماً تيليجرامياً حقيقياً — مرفوض في كل الواجهات
    long_name = "a" + "b" * 33
    assert sender_resolver.parse_telegram_link("@" + long_name).kind == "invalid"
    assert not nav_resolver.is_valid_username(long_name)


# ═══════════════════════ F-28: الكود الميت حُذف ═══════════════════════

def test_f28_dead_code_removed():
    msrc = (PROJECT_DIR / "monitors.py").read_text()
    dsrc = (PROJECT_DIR / "database.py").read_text()
    assert "def build_dynamic_buttons" not in msrc
    assert "def get_sender_access_accounts" not in dsrc


# ═══════════════════════ F-30: إخلاء الأقفال لا يمس المحتجزة ═══════════════════════

def test_f30_eviction_source_pin():
    src = (PROJECT_DIR / "nav_resolver.py").read_text()
    assert "self._discovery_locks.pop(next(iter(self._discovery_locks)))" not in src
    assert "if not lk.locked()" in src


@pytest.mark.asyncio
async def test_f30_eviction_invariant_skips_held_lock():
    """القفل المحتجز لا يُزال — المتزامن الجديد لنفس المرسل يشارك نفس القفل."""
    locks = {i: asyncio.Lock() for i in range(4)}
    await locks[0].acquire()  # قيد الطيران
    try:
        victim = next((k for k, lk in locks.items() if not lk.locked()), None)
        assert victim is not None and victim != 0
        locks.pop(victim)
        assert 0 in locks  # القفل المحتجز باقٍ → single-flight محفوظ
    finally:
        locks[0].release()


# ═══════════════════════ F-18: استمرارية الذاكرة السلبية (SQLite حقيقي) ═══════════════════════

@pytest.mark.asyncio
async def test_f18_tg_user_blocked_survives_restart(db):
    import message_context as mc
    import negative_memory as nm

    mc.reset_tg_user_blocked()
    try:
        assert mc.mark_tg_user_blocked(424242424) is True
        assert mc.is_tg_user_blocked(424242424) is True

        flushed = await nm.flush_now(db)
        assert flushed >= 1

        row = await db._fetchone(
            "SELECT value FROM app_settings WHERE key = ?",
            ("negmem:tg_user_blocked",),
        )
        assert row and "424242424" in str(row["value"])

        # محاكاة إعادة تشغيل: L1 فارغ → ترطيب من DB → الذاكرة حية
        mc.reset_tg_user_blocked()
        assert mc.is_tg_user_blocked(424242424) is False
        await nm.hydrate_all(db)
        assert mc.is_tg_user_blocked(424242424) is True
    finally:
        mc.reset_tg_user_blocked()
        await db._execute("DELETE FROM app_settings WHERE key LIKE 'negmem:%'", ())


@pytest.mark.asyncio
async def test_f18_reply_fix_limits_survive_restart(db):
    import negative_memory as nm
    from reply_fix import ReplyFixer

    f1 = ReplyFixer()
    nm.register_store("reply_fix", f1.snapshot_state, f1.restore_state)
    f1._record_attempt(555000111)

    await nm.flush_now(db, ("reply_fix",))
    row = await db._fetchone(
        "SELECT value FROM app_settings WHERE key = ?",
        ("negmem:reply_fix",),
    )
    assert row and row["value"]

    snap = json.loads(row["value"])
    assert snap["v"] == 1 and "555000111" in snap["users"]

    # مثيل جديد (محاكاة إعادة تشغيل) — قبل الترطيب الحد غير معروف
    f2 = ReplyFixer()
    ok_before, _ = f2.allowed(555000111)
    assert ok_before is True
    f2.restore_state(snap)
    ok_after, reason = f2.allowed(555000111)
    assert ok_after is False and reason == "hourly_cap"

    await db._execute("DELETE FROM app_settings WHERE key LIKE 'negmem:%'", ())


def test_f18_reply_fix_snapshot_drops_stale_entries():
    from reply_fix import ReplyFixer
    import time as _t
    f = ReplyFixer()
    old = _t.monotonic() - 7200.0  # قبل ساعتين — خارج نافذة الساعة
    f._hour_history[42] = __import__("collections").deque([old], maxlen=8)
    snap = f.snapshot_state()
    assert "42" not in snap["users"]  # القديم لا يُخزَّن هدراً


# ═══════════════════════ حواجز مخطط br7: لا أسرار في المستودع ═══════════════════════

def test_br7_migration_files_no_secrets():
    """صفر أسرار في ملفات المخطط/العدّاء — DSN يأتي من بيئة التشغيل حصراً."""
    import re as _re
    runner = (PROJECT_DIR / "scripts" / "apply_br7_schema.py").read_text()
    assert "BR7_APPLY_DSN" in runner          # DSN من البيئة فقط
    assert "pooler.supabase.com" not in runner
    for f in ("migrations/br7_schema_v1.sql", "migrations/validate_br7_v1.sql",
              "scripts/apply_br7_schema.py", "migrations/README.md"):
        t = (PROJECT_DIR / f).read_text()
        # لا DSN حقيقي بكلمة مرور (يتخطى العناصر النائبة <pw>)
        assert not _re.search(r"postgresql://[^\s\"']*:[^\s\"'<]*@", t), f

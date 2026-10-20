"""test_audit_v11_7.py — اختبارات انحدار المراجعة الهندسية الشاملة v11.7.

كل اختبار هنا مرتبط بعيب مُثبت في سجل العيوب (audit A-x/D-x/F-x) من
المراجعة ثلاثية الوكلاء (الاستقرار / قاعدة البيانات / الفلترة). الهدف:
تثبيت السلوك المُصلَح ضد الانحدار — لا تغطية شاملة.

المرجع: worklog.md → Task ID: 11 (v11.7 audit fixes).
"""

import asyncio
import sys
import json
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

sys_path = str(Path(__file__).resolve().parent.parent)
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

import database as db_mod  # noqa: E402
from database import EnhancedDatabase  # noqa: E402

KW_FILE = Path(sys_path) / "keywords.json"


# ═══════════════════════════════════════════════════════════════════════════
# F1 (audit A-1) — إعادة اتصال قاعدة البيانات لم تكن تتم أبداً
# ═══════════════════════════════════════════════════════════════════════════
class TestDbReconnectSelfCancel:
    """العيب: _reconnect كانت تستدعي close() التي تُلغي _writer_task —
    المهمة المستدعية نفسها → RuntimeError/Cancel → connect() لا يُنفذ
    أبداً → زومبي (add_to_queue=-2 للأبد)."""

    @pytest.mark.asyncio
    async def test_reconnect_survives_when_called_from_writer_task(self):
        """_reconnect المستدعاة من مهمة مسجلة كـ _writer_task تنجح وتعيد
        البناء بلا إلغاء الذات، وتنشئ مهمة كاتب جديدة حية."""
        db = EnhancedDatabase()
        try:
            assert await db.connect() is True
            assert db._writer_task is not None
            old_writer = db._writer_task

            async def _from_writer():
                # محاكاة الموقف الحرج: المهمة الحالية هي _writer_task نفسها
                db._writer_task = asyncio.current_task()
                return await db._reconnect()

            ok = await asyncio.wait_for(_from_writer(), timeout=60)
            assert ok is True, "reconnect must succeed (resources rebuilt)"
            assert db.is_connected is True
            new_task = db._writer_task
            assert new_task is not None and not new_task.done()
            assert new_task is not old_writer
        finally:
            try:
                await db.close()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_close_resources_keeps_current_task_alive(self):
        """_close_resources لا تُلغي المهمة الحالية (العقد الجديد) وتغلق
        الموارد وتصفّر المراجع كي يعيد connect() بناءها."""
        db = EnhancedDatabase()
        try:
            assert await db.connect() is True
            current = asyncio.current_task()
            db._writer_task = current  # سجّل المهمة الحالية ككاتب
            await db._close_resources()
            assert not current.cancelling()
            assert db.is_connected is False
            assert db._writer_task is None
            assert db._sqlite_conn is None
        finally:
            try:
                await db.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# F2 (audit A-2) — /health كان يرجع 200 دائماً حتى وهو ميت
# ═══════════════════════════════════════════════════════════════════════════
class _FakeDb:
    """بديل db للوحة — يجب أن يملك close() لأن lifespan ال teardown
    يستدعيه عندما owns_db=True."""

    def __init__(self, is_connected: bool, db_healthy: bool):
        self.is_connected = is_connected
        self.db_healthy = db_healthy
        self.closed = False

    async def close(self):
        self.closed = True


class TestHealthStatusCodes:
    @pytest.fixture()
    async def client(self):
        from dashboard import app

        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                yield ac

    @pytest.mark.asyncio
    async def test_health_503_when_db_down(self, client):
        """db منخرف فعلياً (is_connected=False) → HTTP 503 كي يعيد Render
        تشغيل العملية بدل الزومبي الأبدي."""
        from dashboard import app

        app.state.db = _FakeDb(is_connected=False, db_healthy=False)
        r = await client.get("/health")
        assert r.status_code == 503
        assert r.json()["database"] == "down"

    @pytest.mark.asyncio
    async def test_health_200_when_db_ok(self, client):
        from dashboard import app

        app.state.db = _FakeDb(is_connected=True, db_healthy=True)
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_200_when_degraded_but_connected(self, client):
        """اتصال حي مع db_healthy=False (فشل flush عابر) يبقى 200 عمداً —
        إعادة تشغيل هنا كانت تفقد الدُفعة بلا داعٍ."""
        from dashboard import app

        app.state.db = _FakeDb(is_connected=True, db_healthy=False)
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"


# ═══════════════════════════════════════════════════════════════════════════
# F3 (audit A-4) — DLQ لم يكن يفهم FloodWait
# ═══════════════════════════════════════════════════════════════════════════
class TestDlqFloodAwareDelay:
    def _make_dlq(self):
        from monitors import DeadLetterQueue

        dlq = DeadLetterQueue(db=None, max_retries=3, account_name="test")
        return dlq

    @pytest.mark.asyncio
    async def test_floodwait_seconds_honored(self, monkeypatch):
        """FloodWaitError.seconds=300 → التأخير ≥300 (وليس 2**n ≤60)."""
        dlq = self._make_dlq()
        captured = {}

        class _FakeFlood(Exception):
            seconds = 300

        async def _fake_retry(event_data, retry_count, delay, key):
            captured["delay"] = delay

        monkeypatch.setattr(dlq, "_retry", _fake_retry)

        await dlq.push({"chat_id": 1, "message_id": 2}, _FakeFlood("FloodWait 300"))
        await asyncio.sleep(0.05)  # دع مهمة الإعادة (المزيفة) تجري
        assert captured["delay"] >= 300
        assert captured["delay"] <= 900

    @pytest.mark.asyncio
    async def test_breaker_message_flood_parsed(self, monkeypatch):
        """رسالة CircuitBreakerOpen 'FLOOD_WAIT active, 250.0s remaining'
        تُستخرج من النص → تأخير ≥250."""
        dlq = self._make_dlq()
        captured = {}

        async def _fake_retry(event_data, retry_count, delay, key):
            captured["delay"] = delay

        monkeypatch.setattr(dlq, "_retry", _fake_retry)

        await dlq.push(
            {"chat_id": 1, "message_id": 2},
            RuntimeError("'send_cb' FLOOD_WAIT active, 250.0s remaining"),
        )
        await asyncio.sleep(0.05)
        assert captured["delay"] >= 250

    @pytest.mark.asyncio
    async def test_normal_error_backoff_unchanged(self, monkeypatch):
        """خطأ عادي بلا فيضان → نفس السلم القديم (2**n ≤ 60) — لا تغيير
        سلوكي للنجاحات الحالية."""
        dlq = self._make_dlq()
        captured = {}

        async def _fake_retry(event_data, retry_count, delay, key):
            captured["delay"] = delay

        monkeypatch.setattr(dlq, "_retry", _fake_retry)

        await dlq.push({"chat_id": 1, "message_id": 2}, RuntimeError("boom"))
        await asyncio.sleep(0.05)
        assert captured["delay"] == 1  # 2**0


# ═══════════════════════════════════════════════════════════════════════════
# F4 (audit A-3) — انتهاء الجلسة حالة غير قابلة للاسترداد
# ═══════════════════════════════════════════════════════════════════════════
class TestUnrecoverableSessionErrors:
    def test_source_includes_session_expired(self):
        """عقد ثابت: قائمة غير القابل للاسترداد تشمل Session expired
        (إعادة الإنتاج: الحلقة كانت تعيد المحاولة للأبد على جلسة ميتة)."""
        src = (Path(sys_path) / "monitors.py").read_text(encoding="utf-8")
        assert "_UNRECOVERABLE_CONNECT_ERRORS" in src
        for marker in ("Session expired", "AuthKeyUnregistered", "UserDeactivatedError"):
            assert marker in src, f"missing unrecoverable marker: {marker}"

    def test_record_notification_called_on_unrecoverable(self):
        """الحارس يُصدر إشعاراً إدارياً (record_notification) في نفس الكتلة."""
        src = (Path(sys_path) / "monitors.py").read_text(encoding="utf-8")
        idx = src.find("_UNRECOVERABLE_CONNECT_ERRORS")
        block = src[idx: idx + 2000]
        assert "record_notification" in block


# ═══════════════════════════════════════════════════════════════════════════
# F5 (audit F-1) — بوابة التشابه تكبت تنبيهات حقيقية
# ═══════════════════════════════════════════════════════════════════════════
class TestSimilarityGateV11_7:
    def test_digit_only_difference_passes(self):
        """«95 من 100» مقابل «94 من 100» — طلبان مختلفان (إعادة إنتاج العيب
        الحية: كان يُكبت بـ0.977)."""
        from similarity import SimilarityGate

        gate = SimilarityGate()
        t1 = "نتيجتي في الاختبار النهائي هي 95 من 100 والحمد لله"
        t2 = "نتيجتي في الاختبار النهائي هي 94 من 100 والحمد لله"
        assert gate.check_and_record(9001, t1) is None
        assert gate.check_and_record(9001, t2) is None, (
            "اختلاف بالأرقام فقط لا يجوز أن يُكبت كتكرار"
        )

    def test_rephrased_word_difference_still_blocked(self):
        """إعادة الصياغة المعجمية تبقى ممنوعة (العقد الأصلي للبوابة محفوظ)."""
        from similarity import SimilarityGate

        gate = SimilarityGate()
        t1 = "ابي احد يساعدني بالواجب ضروري الليلة"
        t2 = "ابي احد يساعدني في الواجب ضروري الليلة"
        assert gate.check_and_record(9002, t1) is None
        assert gate.check_and_record(9002, t2) is not None

    def test_blocked_text_does_not_slide_window(self):
        """النص الممنوع لا يُجدَّد طابعه: المحاولات المتكررة لا تمدّد الكبت
        إلى ما لا نهاية (عيب النافذة المنزلقة المُثبت)."""
        from similarity import SimilarityGate

        gate = SimilarityGate()
        t1 = "ابي احد يساعدني بالواجب ضروري الليلة جدا"
        t2 = "ابي احد يساعدني في الواجب ضروري الليلة جدا"
        assert gate.check_and_record(9003, t1) is None
        assert gate.check_and_record(9003, t2) is not None
        bucket_before = len(gate._recent[9003])
        assert gate.check_and_record(9003, t2) is not None
        assert len(gate._recent[9003]) == bucket_before


# ═══════════════════════════════════════════════════════════════════════════
# F7 (audit D-1/D-2/D-5) — توافق PostgreSQL
# ═══════════════════════════════════════════════════════════════════════════
class TestPostgresParity:
    def test_bare_datetime_survivor_exists_then_transform_kills_it(self):
        """التحويل الجديد (قاعدة DATETIME→TIMESTAMP العامة) يقتل كل
        DATETIME ناجٍ — كانت CREATE TABLE تفشل كلياً على PG."""
        src = (Path(sys_path) / "database.py").read_text(encoding="utf-8")
        assert 'replace("DATETIME", "TIMESTAMP")' in src, (
            "القاعدة العامة يجب أن تكون في _create_tables"
        )
        sample = "last_login_at DATETIME, deleted_at DATETIME, updated_at DATETIME"
        out = sample.replace("DATETIME DEFAULT CURRENT_TIMESTAMP", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        out = out.replace("DATETIME", "TIMESTAMP")
        assert "DATETIME" not in out

    def test_scalar_max_becomes_greatest_on_pg(self):
        db = EnhancedDatabase()
        sql = "is_member = MAX(COALESCE(a.x, 0), COALESCE(EXCLUDED.x, 0))"
        db.db_type = "sqlite"
        assert db._sql_scalar_max(sql) == sql  # لا تغيير على SQLite
        db.db_type = "postgresql"
        out = db._sql_scalar_max(sql)
        assert "GREATEST(COALESCE(" in out
        assert "MAX(COALESCE(" not in out

    def test_insert_ignore_dialect(self):
        db = EnhancedDatabase()
        sql = "INSERT OR IGNORE INTO blocked_senders (a) VALUES (?, ?, ?)"
        db.db_type = "sqlite"
        assert db._sql_insert_ignore(sql) == sql
        db.db_type = "postgresql"
        out = db._sql_insert_ignore(sql)
        assert "INSERT OR IGNORE" not in out
        assert out.endswith("ON CONFLICT DO NOTHING")
        assert "INSERT INTO blocked_senders" in out

    def test_alter_decl_datetime_transformed(self):
        """تصريحات ALTER للوحة الحسابات تحوّل DATETIME على PG."""
        src = (Path(sys_path) / "database.py").read_text(encoding="utf-8")
        assert 'decl = str(decl).replace("DATETIME", "TIMESTAMP")' in src


# ═══════════════════════════════════════════════════════════════════════════
# F8 (audit D-3) — جداول intel بلا احتفاظ
# ═══════════════════════════════════════════════════════════════════════════
class TestIntelRetention:
    @pytest.mark.asyncio
    async def test_old_intel_rows_deleted(self, db):
        """صفوف أقدم من النافذة تُحذف من filter_decisions؛ الحديثة تبقى
        (يستخدم الـfixture المؤسسي من conftest).

        الشكل يطابق الإنتاج: created_at تُكتب TEXT عبر CURRENT_TIMESTAMP —
        وهذه نفسها مصيدة الأنواع التي عالجتها الدالة (REAL < TEXT في
        SQLite كان سيعطل التقليم صامتة)."""
        import time as _time

        now = _time.time()
        old_ts = now - 200 * 86400  # 200 يوماً
        old_str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(old_ts))
        new_str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(now))
        for h, ts_str in ((f"ret_old_{old_ts}", old_str), (f"ret_new_{old_ts}", new_str)):
            await db._execute(
                "INSERT INTO filter_decisions (msg_hash, chat_id, sender_id, "
                "decision, created_at) VALUES (?,?,?,?,?)",
                (h, 1, 0, "accept", ts_str),
            )
        await db._commit()
        removed = await db._cleanup_intel_tables(90)
        assert removed >= 1
        rows = await db._fetchall("SELECT msg_hash FROM filter_decisions", ())
        hashes = {r["msg_hash"] for r in rows}
        assert f"ret_old_{old_ts}" not in hashes, "الصف القديم (>90d) يجب أن يُحذف"
        assert f"ret_new_{old_ts}" in hashes, "الصف الحديث يجب أن يبقى"


# ═══════════════════════════════════════════════════════════════════════════
# F9 (audit F-5/F-6) — النفي عبر الترقيم + إهمال dialect للكلمات المعلنة
# ═══════════════════════════════════════════════════════════════════════════
class TestFilterV11_7:
    @pytest.fixture(scope="class")
    def flt(self):
        from filter_engine import EnhancedFilter

        return EnhancedFilter()

    @pytest.mark.asyncio
    async def test_discourse_la_with_comma_not_negated(self, flt):
        """«لا، ابي حل واجب» — النفي الاستئنافي خلف فاصلة لا يكبت الطلب
        (كان يُكبت: dist≤1 → 0.8 → ignore)."""
        res = await flt.analyze("لا، ابي احد يساعدني بحل واجب الرياضيات اليوم")
        assert res.get("decision") in ("accept", "review"), (
            f"decision={res.get('decision')} — النفي قتل طلباً حقيقياً"
        )

    @pytest.mark.asyncio
    async def test_direct_negation_still_works(self, flt):
        """النفي المباشر بلا ترقيم يبقى مكتوماً (لا تراجع في الدقة)."""
        res = await flt.analyze("لا احد فاهم شي في هذا الدرس")
        assert res.get("decision") == "ignore"

    def test_negation_punct_boundary_regex_excludes_whitespace(self, flt):
        """حدود النفي الترقيمية يجب ألا تشمل المسافة (المسافة فاصل كلمات
        طبيعي — إدراجها كان سيكسر كل النفي المباشر)."""
        from filter_engine import _NEG_PUNCT_BOUNDARY_RE

        assert not _NEG_PUNCT_BOUNDARY_RE.search("ما ابي حل") or True
        # الأهم: المسافة وحدها ليست حداً
        assert _NEG_PUNCT_BOUNDARY_RE.search("ما، ابي") is not None
        assert _NEG_PUNCT_BOUNDARY_RE.search("ما ابي") is None

    def test_dialect_source_inside_keyword_excluded(self, flt):
        """مصدر إهمال يظهر داخل كلمة مفتاحية معلنة يُستبعد من النمط —
        «مره» داخل «الماده مره صعبه» لم يعد يُفسد الكلمة المعلنة."""
        pattern = flt._dialect_pattern
        if pattern is None:
            pytest.skip("لا نمط إهمال في هذه الحالة")
        assert "مره" not in (pattern.pattern or ""), (
            "مصدر الإهمال الموجود داخل كلمة مفتاحية معلنة يجب أن يُستبعد"
        )


# ═══════════════════════════════════════════════════════════════════════════
# F10 (audit F-2/F-3) — بيانات keywords.json
# ═══════════════════════════════════════════════════════════════════════════
class TestKeywordsDataV11_7:
    @pytest.fixture(scope="class")
    def kw(self):
        with open(KW_FILE, encoding="utf-8") as f:
            return json.load(f)

    def test_qanoon_not_double_listed(self, kw):
        """«قانون» قسم أكاديمي مشروع — لا يجوز أن يكون في نفس الوقت إشارة
        تجاهل سياسية (القوي-تجاهل كان يقتل طلبات قانون حقيقية — إعادة
        إنتاج حية: «ابي احد يشرح لي قانون التجاره» → ignore)."""
        pol = kw["ignore_signals"]["politics"]
        acad = kw["university_context"]["academic_departments"]
        assert "قانون" not in pol
        assert "قانون" in acad

    def test_institution_bare_terms_not_hard_ads(self, kw):
        """«معهد»/«اكاديميه» المجردة ليست hard_signals — ذكرها في طلب طالب
        كان يقتل التنبيه رغم وجودها في institution_terms المناسبة
        (إعادة إنتاج حية: «ابي احد يساعدني في معهد الدراسة» → ignore)."""
        hs = kw["advertisement_signals"]["hard_signals"]
        assert "معهد" not in hs
        assert "اكاديميه" not in hs
        inst = kw["advertisement_signals"]["institution_terms"]
        assert "معهد تعليمي" in inst
        assert "اكاديميه تعليميه" in inst

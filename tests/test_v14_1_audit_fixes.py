#!/usr/bin/env python3
"""اختبارات إصلاحات التدقيق المعمّق — v14.1.0 (تدقيقا 2-a و 2-b).

يُغطي إصلاحات العيوب المؤكدة من التدقيق المعمّق للفلترة والتنبيهات:
  * [2-a/HIGH] التقرير اليومي: استخدام ثوابت وحدة المستوى BOT_VERSION/BOT_CODENAME
    (كان CFG.BOT_VERSION غير موجود → AttributeError مبتلَع → لا تقرير أبداً).
  * [2-a/MEDIUM] توصيل مفاتيح القتل بالمحرك: FUZZY_MATCHING_ENABLED /
    NEGATION_ENABLED / AD_DETECTION_ENABLED (كانت تُطبع ON/OFF باللوج ولا
    يقرأها المحرك إطلاقاً).
  * [2-a/MEDIUM-latent] إبطال كاش القرارات عند record_feedback و
    reload_keywords (قرارات قديمة مخبأة حتى 5 دقائق).
  * [2-b/MEDIUM] سباق TOCTOU في حصة الرد التعويضي: الحجز قبل الـjitter
    (كان allowed() → sleep → record فيسمح بردّين لنفس المرسل)، وحجز واحد
    للسلّم كاملاً (كان لكل درجة).
  * [2-b/MEDIUM] عداد alerts_sent الصادق: يزداد فقط عند تسليم فعلي
    (المصدر source-level — المسار الحي مغطى باختبارات الانحدار).
  * [2-b/MEDIUM] فرع PostgreSQL في claim_alert_fingerprint بـ
    ON CONFLICT DO NOTHING RETURNING (كان SELECT FOR UPDATE + INSERT
    يفشل مفتوحاً عبر unique_violation عند تعدد النسخ).
  * [2-a/MEDIUM] تيليمتري message_analysis بإصدارات حقيقية.
  * [config] تحقق fail-fast: CONFIDENCE_REVIEW_THRESHOLD < CONFIDENCE_ACCEPT_THRESHOLD.
"""

import asyncio
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

import config as cfg_mod
from config import CFG, BOT_VERSION, BOT_CODENAME

ROOT = Path(__file__).resolve().parent.parent

# ═════════════════════════════ أدوات مساعدة ═════════════════════════════


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _frozen_set(attr: str, value: Any):
    """ضبط حقل في CFG (frozen dataclass) مع إعادة الأصلية بعد الاختبار."""
    original = getattr(CFG, attr)

    def _set(v: Any) -> None:
        object.__setattr__(CFG, attr, v)

    _set(value)

    def _restore() -> None:
        _set(original)

    return _restore


# ═════════════════ 1) التقرير اليومي — إصلاح BOT_VERSION ═════════════════


class TestDigestVersionFix:
    def test_module_constants_exist(self):
        # الثوابت على مستوى الوحدة (CFG frozen بلا هذه الحقول — v14.0)
        assert isinstance(BOT_VERSION, str) and BOT_VERSION
        assert isinstance(BOT_CODENAME, str)

    def test_main_uses_module_constant_not_cfg_attribute(self):
        src = _src("main.py")
        # السطر الجديد يستخدم الثابت المستورد
        assert 'v{BOT_VERSION}' in src
        # لم يعد هناك أي وصول إلى CFG.BOT_VERSION (كان يرمي AttributeError)
        assert "CFG.BOT_VERSION" not in src
        # الثوابت مستوردة فعلاً من config
        assert re.search(r"from config import \([^)]*BOT_VERSION", src, re.S)

    def test_cfg_dataclass_has_no_bot_version_field(self):
        # يثبت سبب العلة الأصلي: الحقل غير موجود على الـdataclass
        assert not hasattr(CFG, "BOT_VERSION")


# ═════════════════ 2) مفاتيح القتل موصولة بالمحرك ═════════════════


@pytest.fixture()
def filter_engine():
    from filter_engine import EnhancedFilter

    return EnhancedFilter()


class TestEngineKillSwitches:
    def test_fuzzy_switch_wired(self, filter_engine):
        """FUZZY_MATCHING_ENABLED=False → المحرك يعطّل fuzzy فعلاً
        (كان يقرأ مفتاحاً غير معرّف فيبقى يعمل رغم الطباعة OFF)."""
        restore = _frozen_set("FUZZY_MATCHING_ENABLED", False)
        try:
            filter_engine.reload_keywords()  # يقرأ المفتاح مجدداً
            assert filter_engine._fuzzy_enabled is False
        finally:
            restore()
            filter_engine.reload_keywords()
        assert filter_engine._fuzzy_enabled is True  # الافتراضي (ON) كما كان

    def test_negation_switch_wired(self, filter_engine):
        """NEGATION_ENABLED=False → كشف النفي معطّل (نتيجة محايدة)."""
        restore = _frozen_set("NEGATION_ENABLED", False)
        try:
            negated, score, reasons = filter_engine._detect_negation("ما ابي حل ابغى كتاب", 3)
            assert negated is False and score == 0.0 and reasons == []
        finally:
            restore()
        # والافتراضي True يكشف النفي كما هو (F-13: «ما ابي حل» نفي قوي 0.8)
        negated, score, _ = filter_engine._detect_negation("ما ابي حل", 7)
        assert negated is True and score >= 0.5

    def test_ad_switch_wired(self, filter_engine):
        """AD_DETECTION_ENABLED=False → كشف الإعلانات محايد تماماً."""
        restore = _frozen_set("AD_DETECTION_ENABLED", False)
        try:
            score, reasons = filter_engine._detect_advertisement("خصم خاص جدد متجر")
            assert score == 0.0 and reasons == []
            # حتى إشارة صلبة (trie) لا تُسقط الرسالة عندما تكون البوابة مطفأة
            monkey_hit = lambda *a, **k: ("sig", 1.0, 0)  # noqa: E731
            original = filter_engine._search_first_valid
            filter_engine._search_first_valid = monkey_hit
            try:
                result = filter_engine._result("accept", 0.9, [])
                assert filter_engine._is_blocked("اي نص", result, ad_score=1.0) is False
            finally:
                filter_engine._search_first_valid = original
        finally:
            restore()

    def test_switches_default_true_preserve_behavior(self, filter_engine):
        """الافتراضي True = السلوك السابق حرفياً (لا تغيير دلالي بدون env)."""
        assert getattr(CFG, "NEGATION_ENABLED", True) is True
        assert getattr(CFG, "AD_DETECTION_ENABLED", True) is True
        assert getattr(CFG, "FUZZY_MATCHING_ENABLED", True) is True


# ═════════════════ 3) إبطال الكاش عند التصحيح/إعادة التحميل ═════════════════


class TestCacheInvalidation:
    @pytest.mark.asyncio
    async def test_record_feedback_clears_text_cache(self, filter_engine):
        filter_engine._text_cache["dead_key"] = {"decision": "accept"}
        await filter_engine.record_feedback("حل", "intent", True)
        assert len(filter_engine._text_cache) == 0

    def test_reload_keywords_clears_text_cache(self, filter_engine):
        filter_engine._text_cache["dead_key"] = {"decision": "accept"}
        filter_engine.reload_keywords()
        assert len(filter_engine._text_cache) == 0


# ═════════════════ 4) حصة الرد التعويضي — سباق TOCTOU ═════════════════


class _SentMessage:
    def __init__(self, entities: List[Any]) -> None:
        self.id = 424242
        self.entities = entities


class MessageEntityMentionName:
    def __init__(self, offset: int = 0, length: int = 2) -> None:
        self.offset = offset
        self.length = length


class _InputUserStub:
    def __init__(self, user_id: int, access_hash: int) -> None:
        self.user_id = user_id
        self.access_hash = access_hash


class _EntityStub:
    def __init__(self, offset: int, length: int, user_id: Any) -> None:
        self.offset = offset
        self.length = length
        self.user_id = user_id


class _FakeClient:
    def __init__(self, *, entities: List[Any] = None, error: Exception = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._entities = entities or []
        self._error = error

    async def send_message(self, chat_id, text, reply_to=None, formatting_entities=None):
        self.calls.append({
            "chat_id": chat_id, "text": text, "reply_to": reply_to,
            "entities": list(formatting_entities or []),
        })
        if self._error is not None:
            raise self._error
        return _SentMessage(entities=self._entities)


UID = 5601276336
HASH = 7234567890123
CHAT = -1001234567890
ALERT_MSG_ID = 555


@pytest.fixture()
def rf_env(monkeypatch):
    import reply_fix as rf

    monkeypatch.setattr(rf, "InputMessageEntityMentionName", _EntityStub)
    monkeypatch.setattr(rf, "InputUser", _InputUserStub)
    monkeypatch.setattr(rf, "_TELETHON_TYPES", True)
    monkeypatch.setattr(rf, "_REPLY_LEN_UTF16", 2)
    return rf


class TestReplyFixQuotaRace:
    @pytest.mark.asyncio
    async def test_quota_reserved_before_jitter(self, rf_env):
        """v14.1: الحجز قبل الـjitter — مهمة ثانية تبدأ أثناء نوم الأولى
        ترى hourly_cap فوراً (كانت تمر بالحصة وترسل ردّاً ثانياً)."""
        from reply_fix import ReplyFixer

        fixer = ReplyFixer(hourly_per_user=1, jitter_min=0.02, jitter_max=0.03)
        client = _FakeClient(entities=[MessageEntityMentionName()])

        task1 = asyncio.create_task(fixer.run(
            client, chat_id=CHAT, reply_to_msg_id=ALERT_MSG_ID,
            user_id=UID, access_hash=HASH,
        ))
        # دع task1 تصل إلى الحجز ثم تنام في الـjitter
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(fixer._hour_history.get(UID, [])) == 1  # محجوزة قبل النوم

        second = await fixer.run(
            client, chat_id=CHAT, reply_to_msg_id=ALERT_MSG_ID,
            user_id=UID, access_hash=HASH,
        )
        assert second.startswith("SKIP:hourly_cap")
        assert await task1 == "OK"
        assert len(client.calls) == 1  # ردّ واحد فقط — لا تكرار

    @pytest.mark.asyncio
    async def test_ladder_consumes_quota_once(self, rf_env):
        """فشل كل درجات السلّم = حجز واحد (كان يُستهلك حصة لكل درجة
        فيقفل المرسل ساعة كاملة حتى بلا ردّ)."""
        from reply_fix import ReplyFixer

        fixer = ReplyFixer(hourly_per_user=1, jitter_min=0, jitter_max=0)
        client = _FakeClient(error=RuntimeError("boom"))
        ladder = [("username", _InputUserStub(UID, HASH)),
                  ("account_hash", _InputUserStub(UID, HASH + 1))]
        res = await fixer.run(
            client, chat_id=CHAT, reply_to_msg_id=ALERT_MSG_ID,
            user_id=UID, access_hash=HASH, ladder=ladder,
        )
        assert res.startswith("FAIL:")
        assert len(fixer._hour_history.get(UID, [])) == 1


# ═════════════════ 5) مصادر source-level لإصلاحات المسار الحي ═════════════════


class TestSourceLevelFixes:
    def test_alerts_sent_honest_counting(self):
        src = _src("monitors.py")
        # المتصل الرئيسي يشترط التسليم الفعلي
        assert "_delivered = await self._send_alert(" in src
        assert re.search(r"if _delivered:\s*\n\s*await self\._inc_stat\(\"alerts_sent\"\)", src)
        # نقاط التسليم الخمس تعيد True:
        # (1) AlertBot نجح (2) verified-after-unknown (3) admin-DM
        # (4) fallback المستخدم (5) نهاية المسار الأساسي
        assert src.count("return True  # v14.1") >= 3
        assert ")\n                    return True\n                try:  # v11.1" in src  # (1)
        assert "return True\n                    logger.info(" in src  # (2)

    def test_pg_claim_uses_on_conflict(self):
        src = _src("database.py")
        assert "ON CONFLICT (fingerprint) DO NOTHING" in src
        assert "RETURNING fingerprint" in src

    def test_telemetry_real_versions(self):
        src = _src("monitors.py")
        assert '"engine_version": str(BOT_VERSION)' in src
        assert 'f"intent-engine-v{FILTER_ENGINE_VERSION}"' in src
        assert '"intent-engine-v13"' not in src

    def test_engine_version_constant(self):
        from filter_engine import FILTER_ENGINE_VERSION

        assert re.match(r"^\d+\.\d+\.\d+$", FILTER_ENGINE_VERSION)

    def test_recovery_uses_unified_policy(self):
        src = _src("monitors.py")
        assert "FilterPolicy.check(cap_text)" in src

    def test_config_review_below_accept_validation(self):
        src = _src("config.py")
        assert "CONFIDENCE_REVIEW_THRESHOLD must be > 0 and" in src

    def test_app_version_bumped(self):
        src = _src("monitors.py")
        assert 'app_version="14.0"' in src
        assert 'app_version="13.0"' not in src

    def test_no_dashboard_refs_in_engine_gates(self):
        """التعليقات المحدثة لا تذكر اللوحة المحذوفة كمصدر ضبط حي."""
        for rel in ("dedup.py", "unified_alerts/gate.py", "unified_alerts/filter_policy.py"):
            src = _src(rel)
            assert "من اللوحة" not in src, rel

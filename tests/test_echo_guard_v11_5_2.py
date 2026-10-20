#!/usr/bin/env python3
"""اختبارات حاجز صدى التنبيهات — v11.5.2 (بلاغ الإنتاج: تكرار التنبيه).

الجذر المُثبَت: القالب الموحد (👤/💬) مضغوط بحيث يقع نص التنبيه ضمن حد
6..65 حرفاً؛ والمراقبة مفتوحة على كل شيء (جدول sources فارغ) → رسالة
التنبيه في مجموعة الهدف تُرى كرسالة جديدة → تصنيفها يحوي الكلمة الأصلية
→ تنبيه جديد يحوي بطاقة سابقة → سلسلة صدى متداخلة.

الإصلاح من ثلاث طبقات مُختبرة هنا:
  1) سجل الهويات الذاتية (register_self_sender/is_self_sender) — رسائل
     بوت التنبيهات وحساباتنا لا تُراقب إطلاقاً.
  2) بصمة القالب (is_alert_card_echo) — أي نص يبدأ بـ«👤:» ويحوي «💬:»
     خلال أول 64 حرفاً = بطاقة منتجرة → يُتجاهل حتى بمرسل مجهول.
  3) سياسة النتيجة المجهولة (unknown_send_result_policy) — timeout لا
     يعيد الإرسال الأعمى (متطلب المواصفة: unknown ≠ confirmed failure)،
     ومع مُحقّق AlertUI يُتحقق من الوصول فعلاً قبل أي fallback.

كذلك _validate_event: الصدى يُرفض قبل بوابة الطول ومن دون لمس أي فلتر،
و_verify_alertbot_delivery: مطابقة النص الظاهر (بعد نزع وسوم HTML وفك
الترميز) على رسائل البوت الصادرة الأخيرة فقط.
"""

import time as _time
from types import SimpleNamespace

import pytest

import monitors
from monitors import (
    EnhancedAccountMonitor,
    is_alert_card_echo,
    is_self_sender,
    register_self_sender,
    unknown_send_result_policy,
)


# ═════════════════ 1) سجل الهويات الذاتية ═════════════════

class TestSelfSenderRegistry:
    def test_register_valid_id(self):
        assert register_self_sender(7886937106) is True
        assert is_self_sender(7886937106) is True

    def test_register_invalid_inputs(self):
        for bad in (0, None, "abc", -5, 1.5, object()):
            assert register_self_sender(bad) is False

    def test_unknown_sender_not_self(self):
        assert is_self_sender(5601276336) in (True, False)  # لا استثناء أبداً
        assert is_self_sender(999999999) is False

    def test_string_digits_accepted(self):
        assert register_self_sender("1630216165") is True
        assert is_self_sender(1630216165) is True


# ═════════════════ 2) بصمة القالب (صدى نصي) ═════════════════

_CARD_L1 = (
    "👤: S\n\n💬:\nالسلام عليكم\n\nاحتاج سكليف معتمد بصحتي\n\n"
    "[ مراسلة ] [ عرض الرسالة ]"
)


class TestAlertCardEcho:
    @pytest.mark.parametrize("card", [
        _CARD_L1,
        "👤: مستخدم\n\n💬:\n",                      # بطاقة مرسل غير محسوم
        "  👤: S\n\n💬:\nنص",                        # مسافات بادئة
        "👤: أحمد (@iesk1)\n\n💬:\nمرحبا",           # اسم مع username
    ])
    def test_cards_detected(self, card):
        assert is_alert_card_echo(card) is True

    @pytest.mark.parametrize("user_text", [
        "السلام عليكم احتاج سكليف معتمد بصحتي",      # رسالة حقيقية
        "شوف 👤 في النص وسط الكلام",                 # 👤 وسط النص (ليس بداية)
        "👤: أحمد",                                  # سطر اسم بلا «💬:»
        "👤 فقط رمز",                                # بداية بالرمز بلا ترويسة
        "",                                           # فارغ
        None,                                         # لا نص
    ])
    def test_user_text_not_echo(self, user_text):
        assert is_alert_card_echo(user_text) is False

    def test_label_far_beyond_64_chars_not_echo(self):
        """«💬:» بعد 64 حرفاً من البداية لا يكفي — حماية من نص مستخدم
        طويل يبدأ صدفة بـ👤:"""
        far = "👤: " + "ا" * 80 + "\n💬:"
        assert is_alert_card_echo(far) is False

    def test_label_within_64_detected(self):
        near = "👤: " + "ا" * 30 + "\n💬:\nنص"
        assert is_alert_card_echo(near) is True


# ═════════════════ 3) بوابة _validate_event ═════════════════

class TestValidateEventEchoGuard:
    @pytest.fixture()
    async def monitor(self, db):
        account = {
            "name": "ECHO_TEST", "api_id": 12345, "api_hash": "test-hash",
            "phone": "+90000000000", "session": "echo_session", "priority": 1,
        }
        return EnhancedAccountMonitor(account, db, None)

    @pytest.mark.asyncio
    async def test_self_sender_skipped(self, monitor):
        register_self_sender(7886937106)
        before = monitor._stats.get("echo_skipped", 0)
        ok = await monitor._validate_event({
            "text": "احتاج سكليف معتمد", "sender_id": 7886937106, "chat_id": -1001234567890,
        })
        assert ok is False
        assert monitor._stats["echo_skipped"] == before + 1

    @pytest.mark.asyncio
    async def test_card_echo_text_skipped_even_from_stranger(self, monitor):
        """بطاقة منتجرة من مرسل غير معروف لنا — تُرفض نصياً (طبقة 2)."""
        before = monitor._stats.get("echo_skipped", 0)
        ok = await monitor._validate_event({
            "text": _CARD_L1, "sender_id": 424242, "chat_id": -1001234567890,
        })
        assert ok is False
        assert monitor._stats["echo_skipped"] == before + 1

    @pytest.mark.asyncio
    async def test_real_user_text_still_passes(self, monitor):
        """رسالة مستخدم حقيقي (وإن ذُكر 👤 وسطها) تمر كالمعتاد."""
        ok = await monitor._validate_event({
            "text": "السلام عليكم احتاج سكليف معتمد بصحتي",
            "sender_id": 424242, "chat_id": -1001234567890,
        })
        assert ok is True

    @pytest.mark.asyncio
    async def test_echo_guard_precedes_length_gate(self, monitor):
        """صدى أطول من 65 حرفاً يُرفض كصدى (echo_skipped) لا كخطأ طول."""
        long_card = _CARD_L1 + "ا" * 100
        before_err = monitor._stats.get("errors", 0)
        ok = await monitor._validate_event({
            "text": long_card, "sender_id": 424242, "chat_id": -1001234567890,
        })
        assert ok is False
        assert monitor._stats.get("errors", 0) == before_err  # لم تلمس بوابة الطول


# ═════════════════ 4) سياسة النتيجة المجهولة ═════════════════

class TestUnknownSendResultPolicy:
    def test_timeout_with_verifier(self):
        assert unknown_send_result_policy("timeout_attempt_1", True) == "verify"

    def test_timeout_without_verifier(self):
        assert unknown_send_result_policy("timeout_attempt_2", False) == "stop"

    def test_network_error_with_verifier(self):
        assert unknown_send_result_policy("network_error:ClientConnectorError", True) == "verify"

    def test_network_error_without_verifier(self):
        assert unknown_send_result_policy("network_error:ServerDisconnectedError", False) == "fallback"

    def test_confirmed_failures_fallback(self):
        for reason in ("build_error:ValueError", "http_429", "BUTTON_USER_INVALID", ""):
            assert unknown_send_result_policy(reason, True) == "fallback"
            assert unknown_send_result_policy(reason, False) == "fallback"


# ═════════════════ 5) _verify_alertbot_delivery ═════════════════

class _FakeUI:
    """عميل AlertUI مزيف — يعيد رسائل محددة من get_messages."""

    def __init__(self, messages):
        self._messages = messages
        self.calls = 0

    async def get_messages(self, entity, limit=10):
        self.calls += 1
        return self._messages[:limit]


def _bot_msg(text: str, age_seconds: float = 5.0, out: bool = True):
    return SimpleNamespace(
        out=out, message=text,
        date=SimpleNamespace(timestamp=lambda: _time.time() - age_seconds),
    )


_CARD_HTML = (
    '👤: <a href="https://t.me/ahmed_99">أحمد محمد</a>\n\n💬:\n'
    "<blockquote>السلام عليكم احتاج سكليف معتمد بصحتي</blockquote>"
)


class TestVerifyAlertbotDelivery:
    @pytest.fixture()
    async def monitor(self, db):
        account = {
            "name": "VERIFY_TEST", "api_id": 12345, "api_hash": "test-hash",
            "phone": "+90000000000", "session": "verify_session", "priority": 1,
        }
        return EnhancedAccountMonitor(account, db, None)

    @pytest.mark.asyncio
    async def test_none_client_is_false(self, monitor):
        assert await monitor._verify_alertbot_delivery(None, "x") is False

    @pytest.mark.asyncio
    async def test_matching_recent_outgoing_detected(self, monitor):
        from dedup import normalize_for_fingerprint
        expected = normalize_for_fingerprint(
            "👤: أحمد محمد\n\n💬:\nالسلام عليكم احتاج سكليف معتمد بصحتي")[:48]
        ui = _FakeUI([_bot_msg(_CARD_HTML)])
        assert await monitor._verify_alertbot_delivery(ui, expected) is True

    @pytest.mark.asyncio
    async def test_stale_message_ignored(self, monitor):
        from dedup import normalize_for_fingerprint
        expected = normalize_for_fingerprint("👤: أحمد\n💬: نص قديم")[:48]
        ui = _FakeUI([_bot_msg(_CARD_HTML, age_seconds=600)])  # 10 دقائق
        assert await monitor._verify_alertbot_delivery(ui, expected) is False

    @pytest.mark.asyncio
    async def test_incoming_message_ignored(self, monitor):
        from dedup import normalize_for_fingerprint
        expected = normalize_for_fingerprint("👤: أحمد\n💬: نص")[:48]
        ui = _FakeUI([_bot_msg(_CARD_HTML, out=False)])  # ليست صادرة من البوت
        assert await monitor._verify_alertbot_delivery(ui, expected) is False

    @pytest.mark.asyncio
    async def test_unrelated_messages_not_matched(self, monitor):
        from dedup import normalize_for_fingerprint
        expected = normalize_for_fingerprint("👤: فلان\n💬: نص مختلف تماماً عن هذا")[:48]
        ui = _FakeUI([_bot_msg("👤: شخص آخر\n\n💬:\nكلام لا علاقة له")])
        assert await monitor._verify_alertbot_delivery(ui, expected) is False

    @pytest.mark.asyncio
    async def test_client_error_is_false(self, monitor):
        class _Broken:
            async def get_messages(self, *a, **kw):
                raise RuntimeError("connection lost")
        assert await monitor._verify_alertbot_delivery(_Broken(), "x") is False


# ═════════════════ 6) إحصاء echo_skipped مكشوف للوحة ═════════════════

class TestStatsExposure:
    @pytest.mark.asyncio
    async def test_get_stats_includes_echo_skipped(self, db):
        account = {
            "name": "STATS_TEST", "api_id": 12345, "api_hash": "test-hash",
            "phone": "+90000000000", "session": "stats_session", "priority": 1,
        }
        m = EnhancedAccountMonitor(account, db, None)
        stats = await m.get_stats()
        assert "echo_skipped" in stats


# ═════════════════ 7) اختيار اعتماديات AlertUI — v11.5.3 ═════════════════
# بلاغ لوج Render: «api_id/api_hash مفقود — عميل أزرار التنبيهات معطّل»
# لأن لا حساباً يحمل is_main. الاختيار الجديد: is_main ثم أول حساب متصل.

class TestPickAlertUIAccount:
    def _mon(self, name, connected, api_id=None, api_hash=None):
        from types import SimpleNamespace as NS
        return NS(account={"name": name, "api_id": api_id or 0,
                           "api_hash": api_hash or ""},
                  is_connected=connected)

    def test_main_account_wins(self):
        from main import _pick_alertui_account
        main_acc = {"name": "MAIN", "is_main": True, "api_id": 1, "api_hash": "h"}
        others = [self._mon("OTHER", True, 2, "x")]
        assert _pick_alertui_account([main_acc], others) is main_acc

    def test_first_connected_with_creds(self):
        from main import _pick_alertui_account
        accs = [{"name": "A"}, {"name": "B"}]
        mons = [self._mon("A", True, 0, ""),      # متصل بلا اعتماديات
                self._mon("B", True, 111, "hh"),  # أول متصل بعتماديات
                self._mon("C", True, 222, "zz")]
        got = _pick_alertui_account(accs, mons)
        assert got is not None and got["name"] == "B"

    def test_disconnected_skipped(self):
        from main import _pick_alertui_account
        mons = [self._mon("A", False, 111, "hh")]
        assert _pick_alertui_account([{"name": "A"}], mons) is None

    def test_no_candidates_none(self):
        from main import _pick_alertui_account
        assert _pick_alertui_account([], []) is None
        assert _pick_alertui_account(None, None) is None

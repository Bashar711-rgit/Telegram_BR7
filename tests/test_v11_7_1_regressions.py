"""اختبارات انحدار v11.7.1 — الإصلاحات الخمسة المثبتة (المهمة الهندسية الموحدة).

كل اختبار هنا كان **يفشل على كود v11.7.0** — مُثبت عبر git worktree عند
9c3a3f0 (10 failed / 2 passed؛ الاثنان الناجحان ضابطان مقصودان r4b/r4c).

r1–r3: جذر sender=0 في antispam (observe allow / confirm_spam refused /
       setup يتجاهل الصفوف القديمة).
r4:    _name_entity_missing يقبل text_mention (Bot API يعيده لوسم
       tg://user?id حين يعرف البوت المستخدم) — r4b/r4c ضابطان.
r5–r6: ذاكرة tg://user السلبية (TTL 6h) + pre-heal برابط المصدر.
r7:    [ALERT] يعرض عدد الأزرار المُسلَّم فعلاً لا المتوقع.
r8:    سلّم الرد التعويضي (درجات InputUser بلا hash).

كل التفاعلات الشبكية مزيّفة — لا نداءات حقيقية إطلاقاً.
"""

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import alert_bot as ab
from alert_bot import AlertBot, _name_entity_missing, _sync_built_buttons
from message_context import (
    is_tg_user_blocked,
    mark_tg_user_blocked,
    reset_tg_user_blocked,
)
from antispam import AntiSpamEngine

from tests.test_alert_bot_v10_8 import _FakeAlertBot, _buttons_of, _data  # noqa: E402

SENDER_ID = 6079171409
CHAT_ID = -100111222333


# ═══════════════════ r1–r3: جذر sender=0 في antispam ═══════════════════
class TestAntispamZeroIdentity:
    @pytest.mark.asyncio
    async def test_r1_observe_zero_identity_always_allow(self):
        """sender_id=0 ليس مستخدماً: قنوات/مجهول/بلا هوية لا تُراقب ولا
        تُحظر — allow فوري وعدّاد no_identity_skipped يزيد."""
        eng = AntiSpamEngine()
        for i in range(6):
            act, info = await eng.observe(0, -1001, "ابغى بحث تخرج شاب")
            assert act == "allow", f"message {i}: zero-identity merged under user 0"
        assert eng.stats["no_identity_skipped"] == 6
        assert eng.stats["watch_added"] == 0
        assert eng.stats["spam_confirmed"] == 0
        assert eng._activities.get(0) is None  # لا تتبع تحت معرّف 0
        # رسائل مختلفة بلا هوية لا تتجمع كـ«مستخدم واحد»
        act, _ = await eng.observe(None, -1002, "نص آخر مختلف تماما")
        assert act == "allow"
        act, _ = await eng.observe("", -1003, "ثالث")
        assert act == "allow"
        assert eng.stats["no_identity_skipped"] == 8

    @pytest.mark.asyncio
    async def test_r2_confirm_spam_zero_refused(self):
        """confirm_spam(0) مرفوض: لا صف spam_ignore دائم وهمي ولا _ignored[0]."""
        eng = AntiSpamEngine()
        await eng.confirm_spam(0, ["سبب تجريبي"], evidence={"k": 1})
        assert 0 not in eng._ignored
        assert eng.stats["no_identity_skipped"] >= 1
        assert eng.stats["spam_confirmed"] == 0

    @pytest.mark.asyncio
    async def test_r3_setup_skips_legacy_zero_rows(self):
        """صفوف sender_id=0 القديمة في spam_ignore لا تُحمّل كـ«مستخدم» —
        المستخدمون الحقيقيون يُحمّلون كما هم."""
        class _Rows:
            async def get_spam_ignored(self, limit=100):
                return [
                    {"sender_id": 0},
                    {"sender_id": 111},
                    {"sender_id": 222},
                ]
        eng = AntiSpamEngine()
        await eng.setup(_Rows())
        assert 0 not in eng._ignored, "legacy zero row loaded as a real user"
        assert 111 in eng._ignored and 222 in eng._ignored


# ═══════════════════ r4: text_mention ليس فشلاً ════════════════════════
class TestNameEntityMention:
    def test_r4_text_mention_at_name_offset_is_not_missing(self):
        """Bot API يعيد text_mention لوسم tg://user?id — الاسم الناجح كان
        يُحكم فاشلاً ويُدمَّر editMessageText (جذر «المشكلة ما زالت قائمة»)."""
        ents = [{"type": "text_mention", "offset": ab._NAME_OFFSET_UTF16,
                 "length": 6, "user": {"id": SENDER_ID}}]
        assert _name_entity_missing(ents) is False

    def test_r4b_text_link_still_accepted(self):
        """ضابط: text_link عند موضع الاسم يبقى ناجحاً كما كان."""
        ents = [{"type": "text_link", "offset": ab._NAME_OFFSET_UTF16,
                 "length": 6, "url": "https://t.me/x"}]
        assert _name_entity_missing(ents) is False

    def test_r4c_wrong_offset_still_missing(self):
        """ضابط: كيان بعيد عن موضع الاسم يبقى «مفقوداً» — الفحص ذكي كما هو."""
        ents = [{"type": "text_mention", "offset": ab._NAME_OFFSET_UTF16 + 50,
                 "length": 6, "user": {"id": SENDER_ID}}]
        assert _name_entity_missing(ents) is True
        assert _name_entity_missing([]) is True
        assert _name_entity_missing(None) is True


# ═══════════════════ r5–r6: ذاكرة tg://user السلبية ════════════════════
class TestTgUserBlockedMemory:
    @pytest.mark.asyncio
    async def test_r5_memory_preheal_anchors_name_to_source_link(self):
        """بعد تسجيل الرفض: التنبيه التالي لنفس المرسل يبدأ بلا زر «مراسلة»
        والاسم مرسيّ على رابط الرسالة المصدر من أول إرسال (لا إرسال مهدور
        ولا تعديل) — tier=T2."""
        mark_tg_user_blocked(SENDER_ID)
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, method, reason = await bot.send(
            _data(sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True
        assert method in ("text_only", "mention_button", "username")
        # إرسال واحد فقط — بلا إعادة بعد رفض (pre-heal)
        assert len(bot.calls) == 1
        kb = _buttons_of(bot.calls[0]["payload"])
        assert all(b.get("text") != "مراسلة" for b in kb), "pre-heal must omit contact button"
        assert bot.last_tier == "T2"

    @pytest.mark.asyncio
    async def test_r5b_no_memory_keeps_tg_user_behavior(self):
        """ضابط: بلا ذاكرت رفض — سلوك tg://user القائم كما هو (زر مراسلة
        tg://user في اللوحة)."""
        reset_tg_user_blocked()
        assert is_tg_user_blocked(SENDER_ID) is False
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, method, _ = await bot.send(
            _data(sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True and method == "mention_button"
        kb = _buttons_of(bot.calls[0]["payload"])
        assert any(b.get("url", "").startswith("tg://user?id=") for b in kb)

    @pytest.mark.asyncio
    async def test_r6_rejection_marks_memory_next_send_single_call(self):
        """رفض BUTTON_USER_INVALID الفعلي يعلّم الذاكرة — التنبيه التالي
        لنفس المرسل محاولة واحدة فقط (لا إرسال مرفوض + إعادة)."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_INVALID"}),
            (200, {"ok": True}),
            (200, {"ok": True}),
        ])
        ok1, _, _ = await bot.send(
            _data(sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok1 is True
        assert is_tg_user_blocked(SENDER_ID) is True
        calls_first = len(bot.calls)
        # التنبيه التالي: pre-heal — محاولة واحدة ناجحة حصراً
        ok2, _, _ = await bot.send(
            _data(sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531012",
            group_link="https://t.me/sultanu1999",
        )
        assert ok2 is True
        assert len(bot.calls) == calls_first + 1


# ═══════════════════ r7: عدّ الأزرار الفعلي ════════════════════════════
class TestEmitAlertLogActualCount:
    @pytest.mark.asyncio
    async def test_r7_alert_log_reports_actual_buttons_after_drop(self):
        """بعد رفض tg://user وإعادة البناء بلا زر «مراسلة»: [ALERT] يعرض
        buttons=1/2 (المُسلَّم فعلاً) لا 2/2 (المتوقع قبل البناء)."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_INVALID"}),
            (200, {"ok": True}),
        ])
        captured: List[str] = []

        class _Capture(cap := object):  # noqa: F841 — placeholder لا يُستخدم
            pass

        original = ab.logger

        class _L:
            def info(self, msg, *a, **k):
                captured.append(str(msg))

            def warning(self, msg, *a, **k):
                pass

            def debug(self, msg, *a, **k):
                pass

            def error(self, msg, *a, **k):
                pass

        ab.logger = _L()
        try:
            ok, _, _ = await bot.send(
                _data(sender_username=None, chat_username="sultanu1999"),
                msg_link="https://t.me/sultanu1999/531011",
                group_link="https://t.me/sultanu1999",
            )
        finally:
            ab.logger = original
        assert ok is True
        lines = [c for c in captured if "[ALERT]" in c]
        assert lines, "no [ALERT] line emitted"
        assert "buttons=1/2" in lines[-1], f"expected delivered 1/2, got: {lines[-1]}"

    def test_r7b_sync_helper_updates_built_buttons(self):
        """_sync_built_buttons يزامن اللوحة الفعلية — الأساس وراء العدّ الصادق."""
        built = {"buttons": [["a", "b"]]}
        _sync_built_buttons(built, {"reply_markup": {"inline_keyboard": [["a"]]}})
        assert built["buttons"] == [["a"]]
        _sync_built_buttons(built, {})
        assert built["buttons"] == []


# ═══════════════════ r8: سلّم الرد التعويضي ════════════════════════════
class _FakeClient:
    """عميل مزيّف — يفشل حسب قائمة أسماء أخطاء بالترتيب ثم ينجح."""

    def __init__(self, failures: List[str]):
        self._failures = list(failures)
        self.sent: List[Dict[str, Any]] = []

    async def send_message(self, chat_id, text, reply_to=None, formatting_entities=None):
        self.sent.append({"chat_id": chat_id, "text": text,
                          "reply_to": reply_to, "ents": formatting_entities})
        if self._failures:
            name = self._failures.pop(0)
            raise type(name, (Exception,), {})(name)
        class _Sent:
            entities = [type("MessageEntityMentionName", (), {})()]
        return _Sent()


class TestReplyFixLadder:
    @pytest.mark.asyncio
    async def test_r8_ladder_tries_tiers_in_order_and_succeeds(self):
        """السلّم يجرّب الدرجات بالترتيب؛ نجاح درجة لاحقة يعيد OK باسمها —
        InputUserFromMessage لا يحتاج access_hash."""
        from reply_fix import reset_reply_fixer, get_reply_fixer
        reset_reply_fixer()
        fixer = get_reply_fixer()
        fixer.jitter_min = 0.0
        fixer.jitter_max = 0.0

        class _IU1:
            pass  # سيُبنى الكيان منه — يفشل بناؤه لا؛ يفشل الإرسال

        client = _FakeClient(failures=["Useridinvalid"])  # الدرجة 1 تفشل بالإرسال
        tiers = [("event_sender_hash", _IU1()), ("input_peer_from_message", object())]
        fails: List[Tuple[str, str]] = []
        res = await fixer.run(
            client, chat_id=-100777, reply_to_msg_id=42,
            user_id=SENDER_ID, access_hash=None,
            owner_account="t", ladder=tiers,
            on_tier_fail=lambda t, e: fails.append((t, e)),
        )
        assert res == "OK"
        assert fails == [("event_sender_hash", "Useridinvalid")]
        assert len(client.sent) == 2  # درجتان جُرّبتا بالترتيب
        reset_reply_fixer()

    @pytest.mark.asyncio
    async def test_r8b_ladder_all_fail_reports_last_error(self):
        """كل الدرجات تفشل → FAIL:آخر خطأ + on_tier_fail لكل درجة — لا
        اعتبار نجاحاً ولا استثناء مرفوع."""
        from reply_fix import reset_reply_fixer, get_reply_fixer
        reset_reply_fixer()
        fixer = get_reply_fixer()
        fixer.jitter_min = 0.0
        fixer.jitter_max = 0.0
        client = _FakeClient(failures=["ErrOne", "ErrTwo"])
        tiers = [("t1", object()), ("t2", object())]
        fails: List[Tuple[str, str]] = []
        res = await fixer.run(
            client, chat_id=-100777, reply_to_msg_id=42,
            user_id=SENDER_ID, access_hash=None,
            owner_account="t", ladder=tiers,
            on_tier_fail=lambda t, e: fails.append((t, e)),
        )
        assert res.startswith("FAIL:")
        assert "ErrTwo" in res
        assert fails == [("t1", "ErrOne"), ("t2", "ErrTwo")]
        reset_reply_fixer()

    @pytest.mark.asyncio
    async def test_r8c_no_ladder_still_requires_hash(self):
        """المسار القديم بلا سلّم يبقى حرفياً: بلا hash → SKIP:no_access_hash."""
        from reply_fix import reset_reply_fixer, get_reply_fixer
        reset_reply_fixer()
        fixer = get_reply_fixer()
        res = await fixer.run(
            _FakeClient([]), chat_id=-100777, reply_to_msg_id=42,
            user_id=SENDER_ID, access_hash=None, owner_account="t",
        )
        assert res == "SKIP:no_access_hash"
        reset_reply_fixer()

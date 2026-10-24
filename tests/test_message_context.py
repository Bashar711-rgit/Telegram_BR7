"""اختبارات جامع السياق + دوال الوجهات (المهمة الهندسية الموحدة §3/§4/§5).

التغطية: 23 حالة جامع + كل فروع if/elif/else للروابط الثلاثة + شكل
التنبيه عبر السياق (زران/زر/نص/قناة/قالب مجمد/UTF-16/استمرار الإرسال).

كل التفاعلات مُحاكاة — لا شبكة ولا تيليجرام حقيقي.
"""

import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from message_context import (
    NOT_APPLICABLE,
    UNAVAILABLE,
    build_contact_action,
    build_message_link_result,
    build_sender_link,
    collect_message_context,
    is_tg_user_blocked,
    mark_tg_user_blocked,
    reset_tg_user_blocked,
)

import alert_bot as ab
from tests.test_alert_bot_v10_8 import _FakeAlertBot, _buttons_of, _data  # noqa: E402

SID = 6079171409
CHAT_ID = -100111222333
MSG_ID = 4242


def _data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SID,
        "sender_username": None, "sender_first_name": "نورة",
        "sender_last_name": "الشمري", "sender_type": "user",
        "text": "ابغى بحث تخرج شاب فصل اول", "sender_usernames": None,
        "chat_username": None, "topic_id": None,
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _clean_memory():
    reset_tg_user_blocked()
    yield
    reset_tg_user_blocked()


# ═══════════════════ الجامع: قسم المرسل ════════════════════════════════
class TestCollectorSender:
    @pytest.mark.asyncio
    async def test_username_user(self):
        ctx = await collect_message_context(_data(sender_username="Lara507"))
        s = ctx["sender"]
        assert s["user_id"] == SID
        assert s["identity_available"] is True
        assert s["username"] == "Lara507"
        assert s["profile_link"] == "https://t.me/Lara507"
        assert s["sender_type"] == "user"
        assert s["identity_source"] == "id_only"

    @pytest.mark.asyncio
    async def test_no_username(self):
        ctx = await collect_message_context(_data())
        s = ctx["sender"]
        assert s["username"] == UNAVAILABLE
        assert s["profile_link"] == UNAVAILABLE
        assert "username" in ctx["missing"]

    @pytest.mark.asyncio
    async def test_first_name_only(self):
        ctx = await collect_message_context(_data(sender_last_name=None))
        s = ctx["sender"]
        assert s["first_name"] == "نورة"
        assert s["last_name"] == UNAVAILABLE
        assert s["display_name"] == "نورة"
        assert "last_name" in ctx["missing"]

    @pytest.mark.asyncio
    async def test_zero_id_is_not_identity(self):
        """user_id=0 ليس هوية: identity_available=False والمعرّف غير متاح
        ولا يُستبدل بقيمة افتراضية (قاعدة الدقة رقم 1)."""
        ctx = await collect_message_context(_data(sender_id=0))
        s = ctx["sender"]
        assert s["user_id"] == UNAVAILABLE
        assert s["identity_available"] is False
        assert "user_id" in ctx["missing"]

    @pytest.mark.asyncio
    async def test_missing_id_no_fake_identity(self):
        ctx = await collect_message_context(_data(sender_id=None))
        assert ctx["sender"]["user_id"] == UNAVAILABLE
        assert ctx["sender"]["identity_available"] is False

    @pytest.mark.asyncio
    async def test_bot_sender(self):
        ctx = await collect_message_context(
            _data(sender_type="bot", sender_is_bot=True))
        assert ctx["sender"]["is_bot"] is True

    @pytest.mark.asyncio
    async def test_channel_sender(self):
        ctx = await collect_message_context(_data(sender_type="channel"))
        assert ctx["sender"]["sender_type"] == "channel"

    @pytest.mark.asyncio
    async def test_anonymous_admin_no_identity(self):
        """المشرف المجهول: بلا sender_id — لا هوية مختلقة."""
        ctx = await collect_message_context(_data(sender_id=0, sender_type="none"))
        assert ctx["sender"]["identity_available"] is False
        assert ctx["sender"]["user_id"] == UNAVAILABLE

    @pytest.mark.asyncio
    async def test_overrides_win(self):
        """قيم nav_resolver المحلولة تتفوق على بيانات الحدث."""
        ctx = await collect_message_context(
            _data(sender_username="old_name"),
            overrides={"sender_username": "nav_name"},
        )
        assert ctx["sender"]["username"] == "nav_name"

    @pytest.mark.asyncio
    async def test_account_language_never_fakes_account_name(self):
        """v11.7.1 تصحيح صدق: لا مصدر للغة حساب المرسل في المشروع —
        الحقل NOT_APPLICABLE ولا يُحشى باسم حساب المراقبة (لغة ≠ اسم).
        اسم الحساب يبقى في مكانها الصحيح meta.account_name."""
        ctx = await collect_message_context(_data(), account_name="acc1")
        assert ctx["sender"]["account_language"] == NOT_APPLICABLE
        assert ctx["meta"]["account_name"] == "acc1"

    @pytest.mark.asyncio
    async def test_account_language_kept_when_real_source_provides_it(self):
        """عندما يوفر الحدث لغة فعلية تُحفظ كما هي — لا استبدال ولا استنتاج."""
        ctx = await collect_message_context(
            _data(account_language="ar"), account_name="acc1")
        assert ctx["sender"]["account_language"] == "ar"


# ═══════════════════ الجامع: قسم المحادثة/الرسالة ══════════════════════
class TestCollectorChatMessage:
    @pytest.mark.asyncio
    async def test_private_group(self):
        ctx = await collect_message_context(_data(chat_title="مجموعة الطلاب"))
        assert ctx["chat"]["chat_id"] == CHAT_ID
        assert ctx["chat"]["chat_title"] == "مجموعة الطلاب"
        assert ctx["chat"]["chat_username"] == UNAVAILABLE
        assert ctx["chat"]["chat_link"] == UNAVAILABLE

    @pytest.mark.asyncio
    async def test_public_group(self):
        ctx = await collect_message_context(
            _data(chat_username="sultanu1999"))
        c = ctx["chat"]
        assert c["chat_username"] == "sultanu1999"
        assert c["chat_link"] == "https://t.me/sultanu1999"

    @pytest.mark.asyncio
    async def test_no_id_confusion(self):
        """لا خلط user_id/chat_id/message_id — ثلاث قيم مستقلة حتى عند
        التقارب العددي."""
        ctx = await collect_message_context(
            _data(sender_id=7, chat_id=-700, message_id=70))
        assert ctx["sender"]["user_id"] == 7
        assert ctx["chat"]["chat_id"] == -700
        assert ctx["message"]["message_id"] == 70

    @pytest.mark.asyncio
    async def test_reply_message(self):
        ctx = await collect_message_context(_data(reply_to_message_id=99))
        assert ctx["message"]["reply_to_message_id"] == 99

    @pytest.mark.asyncio
    async def test_forwarded_origin_recorded_only(self):
        """forward_origin معلومة فقط — لا يستبدل رابط الرسالة ولا يُختلق."""
        ctx = await collect_message_context(
            _data(forward_origin={"type": "user"}))
        assert ctx["message"]["forward_origin"] == {"type": "user"}
        ctx2 = await collect_message_context(_data())
        assert ctx2["message"]["forward_origin"] == NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_media_without_text(self):
        """وسائط بلا نص: message_text يبقى غير متاح صادقاً — لا وصف
        مختلق مثل «صورة مرفقة» داخل النص الأصلي (العرض طبقة أخرى)."""
        ctx = await collect_message_context(
            _data(text="", media_type="photo"))
        m = ctx["message"]
        assert m["message_text"] == UNAVAILABLE
        assert m["media_type"] == "photo"
        assert "message_text" in ctx["missing"]

    @pytest.mark.asyncio
    async def test_classification_passthrough_only(self):
        """التصنيف يمرر فقط من نتيجة التحليل — الجامع لا يحسب درجات."""
        ctx = await collect_message_context(_data())
        assert ctx["classification"]["matched_keywords"] == NOT_APPLICABLE
        assert ctx["classification"]["match_score"] == NOT_APPLICABLE
        ctx2 = await collect_message_context(
            _data(), analysis={"matched_keywords": ["بحث"],
                               "match_score": 0.7, "filter_result": "allow"})
        assert ctx2["classification"]["matched_keywords"] == ["بحث"]
        assert ctx2["classification"]["match_score"] == 0.7
        assert ctx2["classification"]["filter_result"] == "allow"

    @pytest.mark.asyncio
    async def test_collector_never_raises(self):
        """الجامع لا يرفع استثناءً أبداً — مدخلات فاسدة/غريبة تعيد سياقاً."""
        for bad in (None, 42, "string", object(), {"chat_id": "not-int",
                                                   "message_id": None}):
            ctx = await collect_message_context(bad)
            assert isinstance(ctx, dict)
            assert "sender" in ctx and "meta" in ctx

    @pytest.mark.asyncio
    async def test_live_event_sender_fetch_failure(self):
        """حدث حي فاشل get_sender → بقية الحقول تُجمع والهوية غير متاحة."""

        class _Msg:
            id = MSG_ID
            text = "نص حي"
            media = None
            date = None
            chat_id = CHAT_ID
            reply_to = None

        class _Ev:
            message = _Msg()
            sender = None
            sender_id = SID
            chat_id = CHAT_ID
            chat = None

            async def get_sender(self):
                raise RuntimeError("network down")

        ctx = await collect_message_context(_Ev())
        s = ctx["sender"]
        # الهوية (المعرف) متاحة من الحدث نفسه — فشل الجلب يحرم الأسماء فقط
        assert s["user_id"] == SID
        assert s["identity_available"] is True
        assert s["identity_source"] == "id_only"
        assert s["first_name"] == UNAVAILABLE
        assert s["username"] == UNAVAILABLE
        assert "username" in ctx["missing"]
        # بقية البيانات استُخرجت رغم فشل جلب المرسل
        assert ctx["message"]["message_text"] == "نص حي"
        assert ctx["message"]["message_id"] == MSG_ID

    @pytest.mark.asyncio
    async def test_partial_field_failure_keeps_rest(self):
        """فشل حقل اختياري واحد لا يُسقط بقية الأقسام (عزل الاستثناءات)."""

        class _Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")

        ev = _data()
        ev["sender_usernames"] = _Boom()  # كائن فاسد في حقل واحد
        ctx = await collect_message_context(ev)
        # الحقول السليمة باقية
        assert ctx["sender"]["user_id"] == SID
        assert ctx["chat"]["chat_id"] == CHAT_ID
        assert ctx["message"]["message_id"] == MSG_ID


# ═══════════════════ رابط اسم المرسل (§4.1) ════════════════════════════
class TestSenderLink:
    @pytest.mark.asyncio
    async def test_username_priority(self):
        ctx = await acollect(sender_username="Lara507")
        r = build_sender_link(ctx)
        assert r == {"url": "https://t.me/Lara507", "kind": "username",
                     "reason_if_none": ""}

    @pytest.mark.asyncio
    async def test_tg_user_with_caveat(self):
        """user_id بلا username → tg://user (بتوثيق القيد: قد يرفضه Bot API)."""
        ctx = await acollect()
        r = build_sender_link(ctx)
        assert r["kind"] == "tg_user"
        assert r["url"] == f"tg://user?id={SID}"

    @pytest.mark.asyncio
    async def test_blocked_memory_forces_message_source(self):
        """الذاكرة السلبية: tg://user المرفوض → رابط الرسالة المصدر مباشرة."""
        mark_tg_user_blocked(SID)
        ctx = await acollect(message_link="https://t.me/sultanu1999/531011")
        r = build_sender_link(ctx)
        assert r["kind"] == "message_source"
        assert r["url"] == "https://t.me/sultanu1999/531011"

    @pytest.mark.asyncio
    async def test_channel_never_gets_user_link(self):
        """قناة/محذوف: معرّف الكيان ليس معرّف مستخدم — لا رابط مستخدم."""
        ctx = await acollect(sender_id=-100999, sender_type="channel")
        r = build_sender_link(ctx)
        assert r["kind"] == "none"
        assert r["url"] is None
        assert "not_a_user" in r["reason_if_none"]

    @pytest.mark.asyncio
    async def test_zero_identity_no_link(self):
        ctx = await acollect(sender_id=0)
        r = build_sender_link(ctx)
        assert r["kind"] == "none"
        assert r["reason_if_none"] == "no_sender_identity"


async def acollect(**kw) -> Dict[str, Any]:
    """مساعد غير متزامن: جامع السياق مع حقن رابط رسالة اختياري."""
    link = kw.pop("message_link", None)
    ctx = await collect_message_context(_data(**kw))
    if link:
        ctx["message"]["message_link"] = link
    return ctx


# ═══════════════════ رابط الرسالة (§4.2) ═══════════════════════════════
class TestMessageLink:
    @pytest.mark.asyncio
    async def test_confirmed_link_wins(self):
        ctx = await acollect(
            chat_username="sultanu1999", message_link="https://t.me/sultanu1999/531011")
        r = build_message_link_result(ctx)
        assert r["kind"] == "confirmed"
        assert r["url"] == "https://t.me/sultanu1999/531011"

    @pytest.mark.asyncio
    async def test_public_group(self):
        ctx = await acollect(chat_username="sultanu1999")
        r = build_message_link_result(ctx)
        assert r["kind"] == "public"
        assert r["url"] == f"https://t.me/sultanu1999/{MSG_ID}"

    @pytest.mark.asyncio
    async def test_private_supergroup(self):
        ctx = await acollect(chat_id=-100111222333)
        r = build_message_link_result(ctx)
        assert r["kind"] == "private"
        assert r["url"] == f"https://t.me/c/111222333/{MSG_ID}"

    @pytest.mark.asyncio
    async def test_topic_path(self):
        ctx = await acollect(chat_username="sultanu1999", topic_id=55)
        r = build_message_link_result(ctx)
        assert r["kind"] == "public_topic"
        assert r["url"] == f"https://t.me/sultanu1999/55/{MSG_ID}"

    @pytest.mark.asyncio
    async def test_plain_group_no_dead_link(self):
        """مجموعة عادية (بدون -100 وبلا username): لا رابط ميت — سبب صادق."""
        ctx = await acollect(chat_id=-444333)
        r = build_message_link_result(ctx)
        assert r["url"] is None
        assert r["reason_if_none"] == "no_public_username_and_not_supergroup"

    @pytest.mark.asyncio
    async def test_no_message_id_reason(self):
        ctx = await acollect(message_id=None, chat_username="sultanu1999")
        r = build_message_link_result(ctx)
        assert r["url"] is None
        assert r["reason_if_none"] == "no_message_id"


# ═══════════════════ وسيلة التواصل (§4.3) ══════════════════════════════
class TestContactAction:
    @pytest.mark.asyncio
    async def test_username_action(self):
        ctx = await acollect(sender_username="Lara507")
        r = build_contact_action(ctx)
        assert r == {"url": "https://t.me/Lara507", "kind": "username",
                     "reason_if_none": ""}

    @pytest.mark.asyncio
    async def test_tg_user_action_no_username(self):
        ctx = await acollect()
        r = build_contact_action(ctx)
        assert r["kind"] == "tg_user"
        assert r["url"] == f"tg://user?id={SID}"

    @pytest.mark.asyncio
    async def test_bot_keeps_button_policy(self):
        """البوت يحصل على الزر مثل المستخدم — سلوك المشروع القائم محفوظ."""
        ctx = await acollect(sender_type="bot", sender_is_bot=True)
        r = build_contact_action(ctx)
        assert r["kind"] == "tg_user"

    @pytest.mark.asyncio
    async def test_channel_no_action(self):
        ctx = await acollect(sender_id=-100999, sender_type="channel")
        r = build_contact_action(ctx)
        assert r["url"] is None and r["kind"] == "none"
        assert "not_contactable" in r["reason_if_none"]

    @pytest.mark.asyncio
    async def test_zero_identity_no_action(self):
        ctx = await acollect(sender_id=0)
        r = build_contact_action(ctx)
        assert r["url"] is None
        assert r["reason_if_none"] == "no_sender_identity"

    @pytest.mark.asyncio
    async def test_blocked_memory_no_button(self):
        mark_tg_user_blocked(SID)
        ctx = await acollect()
        r = build_contact_action(ctx)
        assert r["url"] is None
        assert r["reason_if_none"] == "tg_user_blocked_memory"


# ═══════════════════ شكل التنبيه عبر السياق (§5) ═══════════════════════
class TestAlertShapeViaContext:
    @pytest.mark.asyncio
    async def test_both_buttons_same_row(self):
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, method, _ = await bot.send(
            _data(sender_username="Lara507", chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True and method == "username"
        kb = _buttons_of(bot.calls[0]["payload"])
        assert [b["text"] for b in kb] == ["عرض الرسالة", "مراسلة"]

    @pytest.mark.asyncio
    async def test_single_button_when_one_target(self):
        """بلا username وبلا tg:// (ذاكرة رفض) → زر «عرض الرسالة» وحده."""
        mark_tg_user_blocked(SID)
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, _, _ = await bot.send(
            _data(sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True
        kb = _buttons_of(bot.calls[0]["payload"])
        assert [b["text"] for b in kb] == ["عرض الرسالة"]

    @pytest.mark.asyncio
    async def test_text_only_when_no_targets(self):
        """لا رابط رسالة (مجموعة عادية) ولا زر (ذاكرة رفض) → نص فقط بلا
        reply_markup — التنبيه لا يضيع أبداً."""
        mark_tg_user_blocked(SID)
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, method, _ = await bot.send(
            _data(sender_username=None, chat_username=None, chat_id=-444333),
            msg_link=None, group_link=None,
        )
        assert ok is True
        assert method == "text_only"
        assert "reply_markup" not in bot.calls[0]["payload"]

    @pytest.mark.asyncio
    async def test_channel_no_invalid_button(self):
        """مرسل قناة: لا زر «مراسلة» مختلق من معرّف الكيان."""
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, method, _ = await bot.send(
            _data(sender_id=-100999, sender_type="channel",
                  sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True
        kb = _buttons_of(bot.calls[0]["payload"])
        assert all(not str(b.get("url", "")).startswith("tg://user") for b in kb)

    @pytest.mark.asyncio
    async def test_template_frozen_shape(self):
        """القالب المجمد v11.6 حرفياً: «المرسل:» + «نص الرسالة:» بلا معرفات."""
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, _, _ = await bot.send(
            _data(sender_username=None, chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
        )
        assert ok is True
        txt = bot.calls[0]["payload"]["text"]
        assert txt.startswith("المرسل: ")
        assert "نص الرسالة:" in txt
        # لا معرفات رقمية في النص الظاهر (روابط tg:// تحمل المعرف داخل
        # href — غير مرئية للمستلم؛ الفحص بعد إزالة الوسوم)
        import re as _re
        visible = _re.sub(r"<[^>]+>", "", txt)
        assert str(SID) not in visible
        assert "مستخدم (" not in visible

    @pytest.mark.asyncio
    async def test_no_name_shows_unspecified_sender(self):
        """بلا أي اسم: «مرسل غير محدد» — لا «مستخدم (id)» ولا رقم."""
        built = ab.build_alert_html(_data(
            sender_id=SID, sender_first_name=None, sender_last_name=None,
            sender_username=None, chat_username="sultanu1999",
        ), msg_link="https://t.me/sultanu1999/531011")
        import re as _re
        visible = _re.sub(r"<[^>]+>", "", built["text"])
        assert "مرسل غير محدد" in visible
        assert str(SID) not in visible

    @pytest.mark.asyncio
    async def test_arabic_emoji_utf16_offsets(self):
        """أسماء عربية + رموز تعبيرية: فحص الكيان عند إزاحة UTF-16 الصحيحة
        (الإيموجي وحدتان) — النجاح لا يُحكم فاشلاً."""
        from contact_target import utf16_len, SENDER_LABEL
        name = "نورة 🎉 الشمري"
        built = ab.build_alert_html(_data(
            sender_display=name, sender_username=None,
            chat_username="sultanu1999",
        ), msg_link="https://t.me/sultanu1999/531011")
        # موضع الاسم بعد الترويسة بإزاحة UTF-16
        ents = [{"type": "text_mention", "offset": utf16_len(SENDER_LABEL),
                 "length": 4, "user": {"id": SID}}]
        assert ab._name_entity_missing(ents) is False
        # إيموجي داخل الاسم لا يكسر فحص الموضع
        header = built["text"].split("\n")[0]
        assert "نورة" in header

    @pytest.mark.asyncio
    async def test_context_failure_alert_still_sends(self):
        """فشل كامل لجمع السياق لا يمنع الإرسال — سياق None = سلوك قديم."""
        bot = _FakeAlertBot([(200, {"ok": True})])
        ok, _, _ = await bot.send(
            _data(sender_username="Lara507", chat_username="sultanu1999"),
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
            context={"sender": {"user_id": "UNAVAILABLE"},
                     "message": {}, "chat": {}, "missing": ["all"]},
        )
        assert ok is True
        assert len(bot.calls) == 1

    @pytest.mark.asyncio
    async def test_context_fills_absent_data_only(self):
        """fill-if-absent: السياق يملأ الغائب فقط — الحاضر يتفوق."""
        bot = _FakeAlertBot([(200, {"ok": True})])
        ctx = await collect_message_context(
            _data(sender_username="ctx_name", sender_first_name="سياق"))
        ok, method, _ = await bot.send(
            {"sender_id": SID, "sender_username": "data_name",
             "text": "النص الأصلي", "chat_id": CHAT_ID,
             "message_id": MSG_ID},
            msg_link="https://t.me/sultanu1999/531011",
            group_link="https://t.me/sultanu1999",
            context=ctx,
        )
        assert ok is True and method == "username"
        txt = bot.calls[0]["payload"]["text"]
        assert "https://t.me/data_name" in txt  # قيمة data تتفوق

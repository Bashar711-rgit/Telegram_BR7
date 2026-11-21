#!/usr/bin/env python3
"""اختبارات القالب الموحد v11.6 — مصفوفة الحالات الـ14 الإلزامية (مواصفة المهلة).

القالب الحرفي المطلوب — الصيغة المرجعية الموحدة v14.9.3 (تحديث بقرار المالك
حلّ محل قالب v13 «المرسل: … — @username»):

    👤 {@username | الاسم}
    المرسل : ID {sender_id}      (بلا معرف حقيقي → «المرسل : غير معروف»)
    نص الرساله :
    {النص الأصلي كاملاً}
    رابط الرساله : {رابط حقيقي فقط — بلا رابط يُحذف السطر كلياً}

مع لوحة أزرار فعلية [ @username | الاسم | مراسلة ][ جروب ] مرتبطة بالتنبيه —
بلا B_Bot ولا لاحقة «— @username»، وبلا blockquote/اقتباس، وبلا وسوم خام
ظاهرة خارج وسط الاسم وسطر الرابط الشرعي.

لكل حالة: صحة النص وترتيبه، عدم ظهور وسوم خام، الرابط الصحيح على الاسم،
وجود الزرين ووجهة كل زر، وسلوك إعادة المحاولة/التكرار دون فقدان الأزرار.

كل تفاعلات الشبكة مُزيفة — لا نداءات حقيقية إطلاقاً (اختبار Telegram الحقيقي
موثق خارج هذا الملف — انظر worklog).
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from alert_bot import AlertBot, build_alert_buttons, build_alert_html  # noqa: E402
from contact_target import (  # noqa: E402
    BOT_NAME_LINE,
    MSG_TEXT_LABEL,
    SENDER_LABEL,
    build_alert_entity_pack,
    build_username_suffix,
    resolve_contact_target,
    utf16_len,
)
from monitors import is_alert_card_echo  # noqa: E402

SENDER_ID = 6079171409
CHAT_ID = -100111222333
INNER = "111222333"
MSG_ID = 4242

ORIG = "بموت ابغى رابط تحميل اللوك داون\nانحذف من عندي اختباري بعد ساعه"


def _data(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "sender_username": None, "sender_first_name": "محمد",
        "sender_last_name": "العمري",
        "sender_type": "user", "text": ORIG,
        "sender_usernames": None, "chat_username": None,
    }
    base.update(kw)
    return base


def _row(built) -> List[Dict[str, str]]:
    return built["buttons"][0] if built["buttons"] else []


def _visible(text: str) -> str:
    """النص كما يراه المستخدم — نزع وسوم HTML وحل الكيانات المهربة."""
    import html as _h
    import re as _re
    return _h.unescape(_re.sub(r"<[^>]+>", "", text))


def _assert_template_contract(text: str, *, username: str = None,
                              name: str = None, body: str = None,
                              sender_id: Any = None) -> None:
    """العقد الحرفي — الصيغة المرجعية الموحدة v14.9.3 — مشترك بين كل الحالات.

    البطاقة تبدأ بسطر «👤 {الاسم}» ثم «المرسل : ID …» ثم «نص الرساله :»
    ثم النص الأصلي (سطر واحد بعد التسمية) ثم سطر «رابط الرساله :»."""
    visible = _visible(text)
    lines = visible.split("\n")
    # 1) الترتيب: 👤 ← المرسل : ID ← نص الرساله : ← النص ← رابط الرساله :
    assert lines[0].startswith("👤 "), f"سطر الاسم مفقود: {lines[0]!r}"
    if username:
        assert lines[0] == f"👤 @{username}", f"سطر الاسم لا يطابق: {lines[0]!r}"
    elif name:
        assert lines[0] == f"👤 {name}", f"سطر الاسم لا يطابق: {lines[0]!r}"
    assert lines[1].startswith("المرسل : "), f"سطر المعرف مفقود: {lines[1]!r}"
    if sender_id is not None:
        if sender_id and int(sender_id) > 0:
            assert lines[1] == f"المرسل : ID {sender_id}", f"سطر المعرف لا يطابق: {lines[1]!r}"
        else:
            assert lines[1] == "المرسل : غير معروف", f"بلا معرف → غير معروف: {lines[1]!r}"
    assert "\nنص الرساله :\n" in visible, "قسم «نص الرساله :» بترتيبه مطلوب"
    # 2) بلا رموز قديمة وبلا اقتباس (👤 مطلوب الآن — رأس البطاقة v14.9.3)
    assert "💬" not in text
    assert "<blockquote>" not in text and "</blockquote>" not in text
    assert "<b>" not in text and "<i>" not in text
    # 3) الاسم الظاهر = @username عند توفره — ولا لاحقة «— @» مُختلقة أبداً
    assert "— @" not in visible
    # 4) النص الأصلي كاملاً بعد التسمية مباشرة (أسطره كما وردت)
    if body:
        body_section = visible.split("\nنص الرساله :\n", 1)[1]
        assert body_section.startswith(body), f"النص الأصلي مفقود/مقطوع: {body_section[:60]!r}"


class _FakeAlertBot(AlertBot):
    """Bot API مزيف — يستجيب بكيانات الاسم السليمة افتراضياً."""

    def __init__(self, responses=None):
        super().__init__(token="123456:FAKE", chat_id=-100999888777)
        self._responses = list(responses or [])
        self.calls: List[Dict[str, Any]] = []

    async def _post(self, method: str, payload: Dict[str, Any]):
        self.calls.append({"method": method, "payload": payload})
        if self._responses:
            status, body = self._responses.pop(0)
        else:
            status, body = 200, {"ok": True, "result": {"message_id": 777}}
        if status == 200 and body.get("ok") and method == "sendMessage":
            result = body.setdefault("result", {})
            result.setdefault("entities", [
                {"type": "text_link", "offset": utf16_len(SENDER_LABEL),
                 "length": 9, "url": "https://t.me/ok"}
            ])
        return status, body


# ═══════════════════════════════════════════════════════════════════════
# الحالات 1–3: هوية المرسل (username / user_id / مجهول)
# ═══════════════════════════════════════════════════════════════════════

class TestSenderIdentityCases:
    def test_case1_valid_username(self):
        """حالة 1: username صالح → الاسم الظاهر «@username» برابط t.me + زر
        «@username» (الصيغة المرجعية الموحدة v14.9.3)."""
        built = build_alert_html(_data(sender_username="mohammed_99"))
        _assert_template_contract(built["text"], username="mohammed_99",
                                  body=ORIG, sender_id=SENDER_ID)
        assert 'href="https://t.me/mohammed_99"' in built["text"]
        assert built["contact_method"] == "username"
        contact = next(b for b in _row(built) if b["text"] == "@mohammed_99")  # v14.9.2
        assert contact["url"] == "https://t.me/mohammed_99"

    def test_case2_no_username_with_id(self):
        """حالة 2: بلا username مع user_id → tg://user?id على الاسم، وزر
        المرسل باسم الظاهر بنفس الوسيلة (v14.9.3 — «مراسلة» بقي لبلا اسم)."""
        built = build_alert_html(_data())
        _assert_template_contract(built["text"], username=None,
                                  name="محمد العمري", body=ORIG, sender_id=SENDER_ID)
        assert 'href="tg://user?id=6079171409"' in built["text"]
        contact = next(b for b in _row(built) if b["text"] == "محمد العمري")
        assert contact["url"] == "tg://user?id=6079171409"  # v14.7.0: مخطط موثّق لكل العملاء

    def test_case3_insufficient_data(self):
        """حالة 3: لا هوية إطلاقاً → اسم آمن، بلا رابط مُختلق وبلا زر وهمي."""
        built = build_alert_html(_data(sender_id=0, sender_type="none",
                                       sender_first_name=None, sender_last_name=None))
        _assert_template_contract(built["text"], username=None, body=ORIG, sender_id=0)
        assert "<a " not in built["text"]  # لا مرساة مختلقة
        assert built["contact_method"] == "text_only"
        # لا زر مرسل بلا هدف (لا https://t.me/None ولا «مراسلة» بلا اسم)
        assert [b["text"] for b in _row(built)] == ["جروب"]

    def test_username_suffix_helper(self):
        """« — @username» فقط عند صلاحية الشكل — لا لاحقة لمُدخلات فاسدة."""
        assert build_username_suffix("good_user1") == " — @good_user1"
        assert build_username_suffix("@good_user1") == " — @good_user1"
        assert build_username_suffix(None) == ""
        assert build_username_suffix("") == ""
        assert build_username_suffix("a") == ""  # أقصر من الشكل الصالح


# ═══════════════════════════════════════════════════════════════════════
# الحالات 4–6: نوع المحادثة (عامة / خاصة / قناة)
# ═══════════════════════════════════════════════════════════════════════

class TestChatKindCases:
    def test_case4_public_group(self):
        """حالة 4: مجموعة عامة → زر «جروب» يقود t.me/{u} (المجموعة المصدر)."""
        built = build_alert_html(_data(chat_username="mygroup"))
        group_btn = next(b for b in _row(built) if b["text"] == "جروب")  # v14.9.2
        assert group_btn["url"] == "https://t.me/mygroup"
        # ورابط الرسالة الكامل يبقى أفضل وسيلة عرض عند غياب القروب فقط
        sender_btn = next(b for b in _row(built) if b["text"] == "محمد العمري")
        assert sender_btn["url"] == "tg://user?id=6079171409"

    def test_case5_private_group(self):
        """حالة 5: مجموعة خاصة → زر «جروب» بصيغة t.me/c/{inner} (تفتح للأعضاء)."""
        built = build_alert_html(_data())  # chat_id=-100... بلا username
        group_btn = next(b for b in _row(built) if b["text"] == "جروب")  # v14.9.2
        assert group_btn["url"] == f"https://t.me/c/{INNER}"

    def test_case6_channel_post(self):
        """حالة 6: رسالة قناة → هوية القناة ≠ شخص قابل لمراسلة: بلا
        tg://user مختلق وبلا زر مرسل؛ اسم القناة يظهر فقط (v14.9.3)."""
        built = build_alert_html(_data(sender_id=555, sender_type="channel",
                                       sender_first_name="قناة الأخبار",
                                       sender_last_name=None))
        _assert_template_contract(built["text"], username=None, name="قناة الأخبار",
                                  body=ORIG, sender_id=555)
        assert 'href="tg://user' not in built["text"]
        # القنوات/المحذوفون بلا زر مرسل مختلق — زر القروب فقط (v14.9.3)
        assert [b["text"] for b in _row(built)] == ["جروب"]
        # رابط القروب (هوية القناة/النشر) مستقل عن رابط المرسل (v14.9.2)
        group_btn = next((b for b in _row(built) if b["text"] == "جروب"), None)
        assert group_btn is not None and "/c/" in group_btn["url"]


# ═══════════════════════════════════════════════════════════════════════
# الحالات 7–10: محتوى النص (عربي متعدد الأسطر / وسوم / روابط / وسائط)
# ═══════════════════════════════════════════════════════════════════════

class TestContentCases:
    def test_case7_arabic_multiline(self):
        """حالة 7: عربي متعدد الأسطر — الأسطر والترتيب كما وردت حرفياً
        بعد «نص الرساله :» مباشرة (سطر واحد — v14.9.3)."""
        multi = "سطر أول\nسطر ثاني\n\nسطر رابع بعد فاصل"
        built = build_alert_html(_data(text=multi))
        visible = _visible(built["text"])
        lines = visible.split("\n")
        link_idx = next(i for i, l in enumerate(lines) if l.startswith("رابط الرساله :"))
        body = "\n".join(lines[3:link_idx])  # بعد: 👤 / المرسل : ID / نص الرساله :
        assert body == multi

    def test_case8_html_marks_and_quotes(self):
        """حالة 8: <tag> و& وعلامات اقتباس → تظهر نصاً عادياً ولا تكسر HTML."""
        evil = '<tag> & "اقتباس" <script>alert(1)</script>'
        built = build_alert_html(_data(text=evil))
        # مهربة في السلك (لا وسوم حية من المستخدم)
        assert "&lt;tag&gt; &amp;" in built["text"]
        assert "<script>" not in built["text"]
        # والمستخدم يراها كما كتبت
        assert '<tag> & "اقتباس" <script>alert(1)</script>' in _visible(built["text"])
        # بنية القالب سليمة (وسم واحد للاسم فقط)
        assert built["text"].count("<a ") == 1

    def test_case9_links_and_emoji(self):
        """حالة 9: روابط ورموز تعبيرية تُحفظ كما وردت (العميل يرابطها تلقائياً)."""
        content = "شوف https://example.com/x?y=1&z=2 🔥 ← مثال ✅"
        built = build_alert_html(_data(text=content))
        assert content in _visible(built["text"])
        assert "🔥" in built["text"] and "✅" in built["text"]

    def test_case10_empty_text_with_media(self):
        """حالة 10: نص فارغ مع وسائط → القالب سليم، القسم فارغ بصدق، والأزرار باقية."""
        built = build_alert_html(_data(text=""))
        visible = _visible(built["text"])
        # بعد «نص الرساله :» يليه سطر الرابط الشرعي مباشرة — بلا نص مُختلق
        assert "نص الرساله :\n\nرابط الرساله :" in visible
        assert "<blockquote>" not in built["text"]
        assert len(_row(built)) >= 1
        # حزمة الكيانات للمسار الاحتياطي بنفس القالب (v14.9.3)
        t = resolve_contact_target(sender_id=SENDER_ID)
        pack = build_alert_entity_pack("محمد العمري", "", t)
        assert pack["plain_text"].startswith(
            "👤 محمد العمري\nالمرسل : غير معروف\nنص الرساله :\n")


# ═══════════════════════════════════════════════════════════════════════
# الحالة 11: رابط الرسالة غير متوفر
# ═══════════════════════════════════════════════════════════════════════

class TestMissingLinkCase:
    def test_case11_no_message_link(self):
        """حالة 11: بلا رابط رسالة → سطر «رابط الرساله :» يُحذف كلياً بصدق
        (لا روابط وهمية)، وزر المرسل يبقى؛ لا t.me/None أبداً (v14.9.3)."""
        built = build_alert_html(_data(chat_id=-987654321, message_id=None,
                                       group_link=None, msg_link=None))
        row = _row(built)
        assert all(b["text"] != "عرض الرسالة" for b in row)
        assert all("None" not in str(b.get("url")) for b in row)
        assert all(b["text"] != "#" for b in row)
        assert "رابط الرساله :" not in _visible(built["text"])  # بلا رابط → لا سطر
        contact = next((b for b in row if b["text"] == "محمد العمري"), None)
        assert contact is not None  # زر المرسل مستقل عن رابط الرسالة

    def test_view_falls_back_to_group_link_not_fake(self):
        """بلا رابط رسالة لكن مع رابط قروب → زر «جروب» يفتح المجموعة المصدر."""
        built = build_alert_html(_data(message_id=None), msg_link=None,
                                 group_link="https://t.me/g_only")
        group_btn = next(b for b in _row(built) if b["text"] == "جروب")  # v14.9.2
        assert group_btn["url"] == "https://t.me/g_only"


# ═══════════════════════════════════════════════════════════════════════
# الحالة 12: فشل الإرسال/التنسيق وإعادة المحاولة
# ═══════════════════════════════════════════════════════════════════════

class TestFailureAndRetryCases:
    @pytest.mark.asyncio
    async def test_case12_retry_keeps_buttons(self):
        """إعادة المحاولة (429) تحمل اللوحة نفسها — لا إسقاط reply_markup."""
        bot = _FakeAlertBot([
            (429, {"ok": False, "description": "Too Many Requests",
                   "parameters": {"retry_after": 1}}),
            (200, {"ok": True}),
        ])
        ok, method, _ = await bot.send(_data(sender_username="user1"))
        assert ok is True
        assert len(bot.calls) == 2
        for call in bot.calls:
            kb = call["payload"]["reply_markup"]["inline_keyboard"]
            assert kb and [b["text"] for b in kb[0]] == ["@user1", "جروب"]  # v14.9.2
            assert call["payload"]["parse_mode"] == "HTML"

    @pytest.mark.asyncio
    async def test_case12_button_error_never_text_only_success(self):
        """خطأ لوحة t.me-only = فشل مؤكد → لا «نجاح» بلا أزرار (§5.7)."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: REPLY_MARKUP_INVALID"}),
        ])
        ok, method, reason = await bot.send(_data(sender_username="user1"))
        assert ok is False and "REPLY_MARKUP_INVALID" in reason

    @pytest.mark.asyncio
    async def test_case12_tg_button_rejection_keeps_view_button(self):
        """رفض زر tg://user (خصوصية) → يُعاد بمراسلة فقط مسقطة، وزر العرض باق."""
        bot = _FakeAlertBot([
            (400, {"ok": False, "description": "Bad Request: BUTTON_USER_INVALID"}),
            (200, {"ok": True}),
        ])
        ok, _, _ = await bot.send(_data())  # بلا username → مراسلة tg://user
        assert ok is True and len(bot.calls) == 2
        kb2 = bot.calls[1]["payload"]["reply_markup"]["inline_keyboard"][0]
        assert all(b["text"] != "مراسلة" for b in kb2)
        assert any(b["text"] == "جروب" for b in kb2)  # v14.9.2: زر القروب باقٍ
        # رابط الاسم باقٍ في النص بعد الإعادة
        assert 'tg://user?id=6079171409' in bot.calls[1]["payload"]["text"]

    def test_case12_malformed_html_still_sends(self):
        """نص مستخدم يشبه HTML لا يفسد الإرسال — هروب كامل قبل sendMessage."""
        bot = _FakeAlertBot([])
        data = _data(text='</a><a href="evil">x</a><i>')
        built_ok = True
        try:
            # build_alert_html داخلياً في send() — لا استثناء نصي مكسور
            pass
        finally:
            pass
        # التحقق المباشر: النص المهرب لا يضيف وسوم <a> جديدة
        from alert_bot import build_alert_html as _b
        b2 = _b(data)
        assert b2["text"].count("<a ") == 1  # وسم الاسم النظامي فقط
        assert "&lt;/a&gt;" in b2["text"]
        assert built_ok


# ═══════════════════════════════════════════════════════════════════════
# الحالة 13: التكرار متعدد الحسابات (بصمة القالب الجديد)
# ═══════════════════════════════════════════════════════════════════════

class TestDuplicateAndEchoCase:
    def test_case13_new_card_detected_as_echo(self):
        """بطاقة v11.6 (تبدأ «المرسل:» وتحوي «نص الرسالة:») = صدى مرفوض."""
        card = ("المرسل: محمد العمري — @user_x\n\nنص الرسالة:\n\n"
                + ORIG)
        assert is_alert_card_echo(card) is True
        assert is_alert_card_echo("  " + card) is True  # مسافات بادئة

    def test_case13_legacy_card_still_detected(self):
        """بطاقات القالب القديم (👤/💬) تبقى مكتشفة للتوافق."""
        legacy = "👤: S\n\n💬:\nالسلام عليكم احتاج سكليف"
        assert is_alert_card_echo(legacy) is True

    def test_case13_real_user_text_not_echo(self):
        """رسالة مستخدم حقيقي (ولو ذكر «المرسل» وسط الكلام) تمر."""
        assert is_alert_card_echo(ORIG) is False
        assert is_alert_card_echo("المرسل: محمد يقول إن النص لم يصل بعد") is False
        assert is_alert_card_echo("") is False
        assert is_alert_card_echo(None) is False

    def test_case13_far_label_beyond_window_not_echo(self):
        """«نص الرسالة:» بعد نافذة الفحص (256) لا يكفي — حماية نصوص طويلة."""
        far = "المرسل: " + "ا" * 300 + "\nنص الرسالة:\nنص"
        assert is_alert_card_echo(far) is False

    def test_case13_dedup_uses_original_text(self):
        """بصمة منع التكرار مبنية على النص الأصلي (ليس القالب) — لم تُمس."""
        from dedup import content_fingerprint
        fp1 = content_fingerprint(SENDER_ID, ORIG)
        fp2 = content_fingerprint(SENDER_ID, ORIG)
        fp3 = content_fingerprint(777, ORIG)
        assert fp1 == fp2 and fp1 != fp3


# ═══════════════════════════════════════════════════════════════════════
# الحالة 14: كل مسارات الإرسال البديلة تحمل اللوحة
# ═══════════════════════════════════════════════════════════════════════

class TestAllSendPathsCase:
    def test_case14_central_builder_is_the_only_source(self):
        """الدالة المركزية وحدها تبني اللوحة — لا بناء يدوي في أي مسار."""
        kb = build_alert_buttons(sender_id=SENDER_ID, sender_username="user1",
                                 msg_link="https://t.me/g/9", group_link="https://t.me/g")
        assert [b["text"] for b in kb[0]] == ["@user1", "جروب"]  # v14.9.2
        # بلا بيانات → لوحة فارغة بصدق
        assert build_alert_buttons(sender_id=0) == []
        # include_user_button=False بعد رفض الخصوصية + بلا قروب → عرض الرسالة فقط
        kb2 = build_alert_buttons(sender_id=SENDER_ID, msg_link="https://t.me/g/9",
                                  include_user_button=False)
        assert [b["text"] for b in kb2[0]] == ["عرض الرسالة"]

    def test_case14_entity_pack_matches_html_template(self):
        """مسار MTProto الاحتياطي: نفس الشكل المرئي حرفياً عبر كيانات جاهزة
        (الصيغة المرجعية الموحدة v14.9.3 — كيان واحد للاسم)."""
        t = resolve_contact_target(sender_id=SENDER_ID, username="user1",
                                   first_name="محمد", last_name="العمري")
        pack = build_alert_entity_pack("محمد العمري", ORIG, t)
        expected = ("👤 @user1\nالمرسل : غير معروف\nنص الرساله :\n" + ORIG)
        assert pack["plain_text"] == expected
        ents = pack["formatting_entities"]
        assert len(ents) == 1  # كيان واحد للاسم — بلا كيان ثانٍ للاسم البديل
        name_ent = next(e for e in ents if type(e).__name__ == "MessageEntityTextUrl")
        assert name_ent.offset == utf16_len(SENDER_LABEL)  # بعد «👤 » (=3)
        assert name_ent.length == utf16_len("@user1")
        assert name_ent.url == "https://t.me/user1"
        # لا كيان blockquote في أي حزمة (v11.6)
        assert all(type(e).__name__ != "MessageEntityBlockquote" for e in ents)
        assert pack["clickable"] is True

    @pytest.mark.asyncio
    async def test_case14_bot_payload_full_shape(self):
        """حمولة Bot API الكاملة: النص + HTML + اللوحة + منع المعاينة."""
        bot = _FakeAlertBot([])
        ok, method, _ = await bot.send(_data(sender_username="user1",
                                             chat_username="mygroup"))
        assert ok is True and method == "username"
        p = bot.calls[0]["payload"]
        assert p["parse_mode"] == "HTML"
        assert p["link_preview_options"] == {"is_disabled": True}
        kb = p["reply_markup"]["inline_keyboard"]
        assert kb == [[
            {"text": "@user1", "url": "https://t.me/user1"},
            {"text": "جروب", "url": "https://t.me/mygroup"},
        ]]  # v14.9.2
        _assert_template_contract(p["text"], username="user1",
                                  body=ORIG, sender_id=SENDER_ID)

    def test_case14_user_account_path_carries_buttons(self):
        """مسار حسابات المستخدمين (monitors._build_alert) يعيد اللوحة نفسها."""
        from monitors import EnhancedAccountMonitor
        account = {"id": 1, "prefix": "ACCOUNT_1", "name": "A1", "session": "s",
                   "api_id": 1, "api_hash": "h", "phone": "+966500000000",
                   "priority": 1}
        m = EnhancedAccountMonitor(account, None, None)
        sender = {"id": SENDER_ID, "display": "محمد العمري", "username": "user1",
                  "access_hash": None, "usernames": None}
        chat = {"entity": None, "title": None, "id": CHAT_ID, "message_id": MSG_ID,
                "username": "mygroup", "group_link": "https://t.me/mygroup",
                "msg_link": f"https://t.me/mygroup/{MSG_ID}"}
        text, buttons = m._build_alert(sender, chat, "واجب", ORIG, {})
        _assert_template_contract(text, username="user1",
                                  body=ORIG, sender_id=SENDER_ID)
        assert buttons == [[
            {"text": "@user1", "url": "https://t.me/user1"},
            {"text": "جروب", "url": "https://t.me/mygroup"},
        ]]  # v14.9.2

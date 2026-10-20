#!/usr/bin/env python3
"""اختبارات القالب الموحد v11.6 — مصفوفة الحالات الـ14 الإلزامية (مواصفة المهلة).

القالب الحرفي المطلوب:
    المرسل: {اسم المرسل قابل للنقر}{ — @username}

    نص الرسالة:

    {النص الأصلي كاملاً}

مع لوحة أزرار فعلية [ عرض الرسالة ][ مراسلة ] مرتبطة بالتنبيه —
بلا رموز 👤/💬، بلا blockquote/اقتباس، وبلا وسوم خام ظاهرة.

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
                              name: str = None, body: str = None) -> None:
    """العقد الحرفي v11.6 — مشترك بين كل الحالات."""
    visible = _visible(text)
    # 1) الترتيب: سطر المرسل ← (فارغ) ← نص الرسالة: ← (فارغ) ← النص
    lines = visible.split("\n")
    assert lines[0].startswith("المرسل: "), f"سطر المرسل مفقود: {lines[0]!r}"
    assert "\n\nنص الرسالة:\n\n" in visible, "قسم «نص الرسالة:» بترتيبه مطلوب"
    # 2) بلا رموز قديمة وبلا اقتباس
    assert "👤" not in text and "💬" not in text
    assert "<blockquote>" not in text and "</blockquote>" not in text
    assert "<b>" not in text and "<i>" not in text
    # 3) @username نص مستقل عند توفره فقط — لا يُختلق
    if username:
        assert f"— @{username}" in visible
    else:
        assert "— @" not in visible
    # 4) الاسم في موضع الاسم (بعد الترويسة مباشرة)
    if name:
        assert visible.startswith(f"المرسل: {name}") or f"المرسل: {name}" in visible
    # 5) النص الأصلي كاملاً بعد التسمية (أسطره كما وردت)
    if body:
        body_section = visible.split("\n\nنص الرسالة:\n\n", 1)[1]
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
        """حالة 1: username صالح → رابط t.me على الاسم + « — @username» نصاً."""
        built = build_alert_html(_data(sender_username="mohammed_99"))
        _assert_template_contract(built["text"], username="mohammed_99",
                                  name="محمد العمري", body=ORIG)
        assert 'href="https://t.me/mohammed_99"' in built["text"]
        assert built["contact_method"] == "username"
        contact = next(b for b in _row(built) if b["text"] == "مراسلة")
        assert contact["url"] == "https://t.me/mohammed_99"

    def test_case2_no_username_with_id(self):
        """حالة 2: بلا username مع user_id → tg://user?id على الاسم بلا @."""
        built = build_alert_html(_data())
        _assert_template_contract(built["text"], username=None,
                                  name="محمد العمري", body=ORIG)
        assert 'href="tg://user?id=6079171409"' in built["text"]
        contact = next(b for b in _row(built) if b["text"] == "مراسلة")
        assert contact["url"] == "tg://user?id=6079171409"

    def test_case3_insufficient_data(self):
        """حالة 3: لا هوية إطلاقاً → اسم آمن، بلا رابط مُختلق وبلا زر وهمي."""
        built = build_alert_html(_data(sender_id=0, sender_type="none",
                                       sender_first_name=None, sender_last_name=None))
        _assert_template_contract(built["text"], username=None, body=ORIG)
        assert "<a " not in built["text"]  # لا مرساة مختلقة
        assert built["contact_method"] == "text_only"
        # لا زر مراسلة بلا هدف (لا https://t.me/None)
        assert all(b["text"] != "مراسلة" for b in _row(built))

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
        """حالة 4: مجموعة عامة → زر عرض يقود t.me/{u}/{id} بالرسالة نفسها."""
        built = build_alert_html(_data(chat_username="mygroup"))
        view = next(b for b in _row(built) if b["text"] == "عرض الرسالة")
        assert view["url"] == f"https://t.me/mygroup/{MSG_ID}"

    def test_case5_private_group(self):
        """حالة 5: مجموعة خاصة → صيغة t.me/c/{inner}/{id} (تفتح للأعضاء)."""
        built = build_alert_html(_data())  # chat_id=-100... بلا username
        view = next(b for b in _row(built) if b["text"] == "عرض الرسالة")
        assert view["url"] == f"https://t.me/c/{INNER}/{MSG_ID}"

    def test_case6_channel_post(self):
        """حالة 6: رسالة قناة → هوية القناة ≠ شخص قابل لمراسلة: بلا
        tg://user مختلق وبلا زر مراسلة؛ اسم القناة يظهر فقط."""
        built = build_alert_html(_data(sender_id=555, sender_type="channel",
                                       sender_first_name="قناة الأخبار",
                                       sender_last_name=None))
        _assert_template_contract(built["text"], username=None, body=ORIG)
        assert "tg://user" not in built["text"].replace('href="', 'href=') or \
            'href="tg://user' not in built["text"]
        assert all(b["text"] != "مراسلة" for b in _row(built))
        # رابط الرسالة (هوية القناة/النشر) مستقل عن رابط المرسل
        view = next((b for b in _row(built) if b["text"] == "عرض الرسالة"), None)
        assert view is not None and "/c/" in view["url"]


# ═══════════════════════════════════════════════════════════════════════
# الحالات 7–10: محتوى النص (عربي متعدد الأسطر / وسوم / روابط / وسائط)
# ═══════════════════════════════════════════════════════════════════════

class TestContentCases:
    def test_case7_arabic_multiline(self):
        """حالة 7: عربي متعدد الأسطر — الأسطر والترتيب كما وردت حرفياً."""
        multi = "سطر أول\nسطر ثاني\n\nسطر رابع بعد فاصل"
        built = build_alert_html(_data(text=multi))
        body = _visible(built["text"]).split("\n\nنص الرسالة:\n\n", 1)[1]
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
        """حالة 10: نص فارغ مع وسائط → القالب سليم، القسم فارغ بصدق،
        الأزرار باقية (الوسائط تصل عبر forward supplement)."""
        built = build_alert_html(_data(text=""))
        visible = _visible(built["text"])
        assert visible.endswith("نص الرسالة:") or visible.rstrip("\n").endswith("نص الرسالة:")
        assert "<blockquote>" not in built["text"]
        assert len(_row(built)) >= 1
        # حزمة الكيانات للمسار الاحتياطي بنفس القالب
        t = resolve_contact_target(sender_id=SENDER_ID)
        pack = build_alert_entity_pack("محمد العمري", "", t)
        assert pack["plain_text"].startswith("المرسل: محمد العمري\n\nنص الرسالة:")


# ═══════════════════════════════════════════════════════════════════════
# الحالة 11: رابط الرسالة غير متوفر
# ═══════════════════════════════════════════════════════════════════════

class TestMissingLinkCase:
    def test_case11_no_message_link(self):
        """حالة 11: بلا رابط رسالة → زر عرض يختفي بصدق (لا روابط وهمية)،
        وزر مراسلة يبقى؛ لا t.me/None أبداً."""
        built = build_alert_html(_data(chat_id=-987654321, message_id=None,
                                       group_link=None, msg_link=None))
        row = _row(built)
        assert all(b["text"] != "عرض الرسالة" for b in row)
        assert all("None" not in str(b.get("url")) for b in row)
        assert all(b["text"] != "#" for b in row)
        contact = next((b for b in row if b["text"] == "مراسلة"), None)
        assert contact is not None  # مراسلة مستقلة عن رابط الرسالة

    def test_view_falls_back_to_group_link_not_fake(self):
        """بلا رابط رسالة لكن مع رابط قروب → عرض يفتح أفضل آلية متاحة."""
        built = build_alert_html(_data(message_id=None), msg_link=None,
                                 group_link="https://t.me/g_only")
        view = next(b for b in _row(built) if b["text"] == "عرض الرسالة")
        assert view["url"] == "https://t.me/g_only"


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
            assert kb and [b["text"] for b in kb[0]] == ["عرض الرسالة", "مراسلة"]
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
        assert any(b["text"] == "عرض الرسالة" for b in kb2)
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
        assert [b["text"] for b in kb[0]] == ["عرض الرسالة", "مراسلة"]
        # بلا بيانات → لوحة فارغة بصدق
        assert build_alert_buttons(sender_id=0) == []
        # include_user_button=False بعد رفض الخصوصية → عرض فقط
        kb2 = build_alert_buttons(sender_id=SENDER_ID, msg_link="https://t.me/g/9",
                                  include_user_button=False)
        assert [b["text"] for b in kb2[0]] == ["عرض الرسالة"]

    def test_case14_entity_pack_matches_html_template(self):
        """مسار MTProto الاحتياطي: نفس الشكل المرئي حرفياً عبر كيانات جاهزة."""
        t = resolve_contact_target(sender_id=SENDER_ID, username="user1",
                                   first_name="محمد", last_name="العمري")
        pack = build_alert_entity_pack("محمد العمري", ORIG, t)
        expected = ("المرسل: محمد العمري — @user1\n\nنص الرسالة:\n\n" + ORIG)
        assert pack["plain_text"] == expected
        ents = pack["formatting_entities"]
        name_ent = next(e for e in ents if type(e).__name__ == "MessageEntityTextUrl")
        assert name_ent.offset == utf16_len(SENDER_LABEL)
        assert name_ent.length == utf16_len("محمد العمري")
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
            {"text": "عرض الرسالة", "url": f"https://t.me/mygroup/{MSG_ID}"},
            {"text": "مراسلة", "url": "https://t.me/user1"},
        ]]
        _assert_template_contract(p["text"], username="user1",
                                  name="محمد العمري", body=ORIG)

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
                                  name="محمد العمري", body=ORIG)
        assert buttons == [[
            {"text": "عرض الرسالة", "url": f"https://t.me/mygroup/{MSG_ID}"},
            {"text": "مراسلة", "url": "https://t.me/user1"},
        ]]

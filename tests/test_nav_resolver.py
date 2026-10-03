"""Alert Navigation Resolver tests (v10.5 — الحلّ متعدد الاستراتيجيات).

يغطي السلّم الكامل للإجراءات الثلاثة في التنبيه:
  * اسم المرسل:  حدث ← كاش الكيانات المشترك ← DB ← مرساة tg:// الأصلية
  * اسم المجموعة: كيان ← username الحدث ← صيغة c/‏-100 ← exportMessageLink
  * عرض الرسالة: t.me/{u}/{id} ← t.me/c/{inner}/{id} ← نتيجة export ← «غير متاح»

وعقد التجميد: كل هذا يغيّر البيانات فقط — لا الشكل. اختبارات العقد الذهبية
(test_alert_regression / test_buttons / golden_alert_baseline) يجب أن تبقى
خضراء دون أي تعديل.

سيناريوهات مواصفة المهمة (8 حالات تمثيلية):
  مجموعة عامة / مجموعة فائقة خاصة / قناة / مرسل معه username / مرسل بلا
  username / access_hash مفقود / محادثة يتعذّر الوصول لها / فشل الاستراتيجية
  الأولى مع استمرار البديل.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from config import CFG  # noqa: E402
from monitors import EnhancedAccountMonitor, build_dynamic_buttons  # noqa: E402
from nav_resolver import (  # noqa: E402
    NavReport,
    NavigationResolver,
    build_canonical_links,
    clean_username,
    is_valid_tme_url,
    is_valid_username,
    private_supergroup_inner_id,
)
from nav_resolver import nav_resolver as singleton  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# مساعدات خفيفة
# ═══════════════════════════════════════════════════════════════════════════
class FakeChannel:
    """كيان مجموعة فائقة/قناة — ما يعيده get_entity للمحادثات ذات -100."""

    def __init__(self, chat_id: int, username: Optional[str], title: str = "ق"):
        self.id = chat_id
        self.username = username
        self.title = title
        self.access_hash = 987654321


class FakeBasicChat:
    """كيان مجموعة أساسية (قديمة) — لا username ولا روابط رسائل أبداً."""

    def __init__(self, chat_id: int, title: str = "مجموعة قديمة"):
        self.id = chat_id
        self.title = title
        self.username = None


class FakeUser:
    def __init__(self, user_id: int, username: Optional[str] = None, access_hash: int = 42):
        self.id = user_id
        self.username = username
        self.access_hash = access_hash
        self.first_name = "أ"


class FakeDB:
    def __init__(self, contact: Optional[Dict[str, Any]] = None):
        self.contact = contact or {}
        self.calls = 0

    async def get_sender_contact(self, sender_id: int) -> Optional[Dict[str, Any]]:
        self.calls += 1
        return self.contact or None


class FakeResolverIntel:
    """بديل sender_intel لكاش الكيانات المشترك."""

    def __init__(self, entities: Optional[Dict[int, Any]] = None):
        self._entities = entities or {}

    def cached_username(self, sender_id: Any) -> Optional[str]:
        ent = self._entities.get(int(sender_id or 0))
        return getattr(ent, "username", None) if ent is not None else None


class ExportClient:
    """عميل وهمي يدعم exportMessageLink فقط (آخر ملذ في السلّم)."""

    def __init__(self, link: Optional[str] = None, fail: bool = False):
        self.link = link
        self.fail = fail
        self.calls: List[int] = []

    async def get_input_entity(self, key: Any):
        return FakeChannel(int(key), username=None)

    async def __call__(self, req: Any):
        self.calls.append(getattr(req, "id", 0))
        if self.fail:
            raise RuntimeError("CHANNEL_INVALID")
        return SimpleNamespace(link=self.link)


@pytest.fixture
def resolver() -> NavigationResolver:
    return NavigationResolver()


@pytest.fixture
def monitor() -> EnhancedAccountMonitor:
    account = {
        "name": "NavAcc", "api_id": 12345, "api_hash": "h",
        "phone": "+90000000000", "session": "nav", "priority": 1,
    }
    return EnhancedAccountMonitor(account, db=None, flt=None)


def _base_data(chat_id: int = -1001234567890, message_id: int = 55) -> Dict[str, Any]:
    return {"chat_id": chat_id, "message_id": message_id, "sender_id": 777000111}


# ═══════════════════════════════════════════════════════════════════════════
# 1) المساعدات الصرفة — قواعد بناء الروابط (لا روابط مخترعة أبداً)
# ═══════════════════════════════════════════════════════════════════════════
class TestPureHelpers:
    def test_inner_id_only_for_minus100_shape(self):
        assert private_supergroup_inner_id(-1001234567890) == "1234567890"
        assert private_supergroup_inner_id(-1234567) is None          # مجموعة أساسية
        assert private_supergroup_inner_id(123456) is None            # إيجابي غير منطقي
        assert private_supergroup_inner_id(-100) is None              # حافة فارغة
        assert private_supergroup_inner_id(None) is None
        assert private_supergroup_inner_id("-100abc") is None

    def test_username_validation(self):
        assert clean_username("@User_Name") == "User_Name"
        assert is_valid_username("ahmed_99") is True
        assert is_valid_username("9999") is False       # أرقام فقط ليس username
        assert is_valid_username("a b") is False        # فراغات
        assert is_valid_username(None) is False
        assert is_valid_username("") is False

    def test_tme_url_validation(self):
        assert is_valid_tme_url("https://t.me/mygroup") is True
        assert is_valid_tme_url("https://t.me/mygroup/123") is True
        assert is_valid_tme_url("https://t.me/c/1234567890/456") is True
        assert is_valid_tme_url("https://t.me/c/-1234567/456") is False   # سالب = مكسور
        assert is_valid_tme_url("https://t.me//456") is False
        assert is_valid_tme_url("https://t.me/") is False
        assert is_valid_tme_url("#") is False
        assert is_valid_tme_url("http://t.me/g/1") is False  # لا https
        assert is_valid_tme_url("https://t.me/g/abc") is False  # msg ليس رقماً

    def test_canonical_links_public(self):
        links = build_canonical_links(-1001234567890, 789, "eng_group")
        assert links == {"group": "https://t.me/eng_group", "message": "https://t.me/eng_group/789"}

    def test_canonical_links_private(self):
        links = build_canonical_links(-1001234567890, 456, None)
        assert links == {"group": "https://t.me/c/1234567890", "message": "https://t.me/c/1234567890/456"}

    def test_canonical_links_basic_group_never_invents(self):
        # مجموعة أساسية بلا username → لا رابط شرعي — "#" حرفياً
        links = build_canonical_links(-1234567, 42, None)
        assert links == {"group": "#", "message": "#"}
        # username فاسد يُرفض ولا يُبنى منه رابط
        links = build_canonical_links(-1234567, 42, "99_bad name")
        assert links == {"group": "#", "message": "#"}


# ═══════════════════════════════════════════════════════════════════════════
# 2) سلم المرسل — S1 حدث ← S2 كاش ← S3 DB
# ═══════════════════════════════════════════════════════════════════════════
class TestSenderLadder:
    async def test_s1_event_username_wins(self, resolver, monitor):
        data = _base_data()
        data["sender_username"] = "ahmed_99"
        got = await resolver._resolve_sender_username(monitor, data, NavReport())
        assert got == "ahmed_99"

    async def test_s2_shared_entity_cache(self, resolver, monitor, monkeypatch):
        data = _base_data()
        data["sender_username"] = None
        fake_intel = FakeResolverIntel({777000111: FakeUser(777000111, "cached_user")})
        monkeypatch.setitem(sys.modules, "sender_resolver",
                            SimpleNamespace(sender_intel=fake_intel))
        got = await resolver._resolve_sender_username(monitor, data, NavReport())
        assert got == "cached_user"
        # COALESCE داخل بيانات الحدث — يغذّي درجة username في سلّم mention
        assert data["sender_username"] == "cached_user"

    async def test_s3_db_contact_row(self, resolver, monitor, monkeypatch):
        data = _base_data()
        data["sender_username"] = None
        fake_intel = FakeResolverIntel({})
        monkeypatch.setitem(sys.modules, "sender_resolver",
                            SimpleNamespace(sender_intel=fake_intel))
        monitor.db = FakeDB({"username": "from_db"})
        got = await resolver._resolve_sender_username(monitor, data, NavReport())
        assert got == "from_db"
        assert data["sender_username"] == "from_db"

    async def test_all_miss_returns_none_anchor_fallback(self, resolver, monitor, monkeypatch):
        data = _base_data()
        data["sender_username"] = None
        fake_intel = FakeResolverIntel({})
        monkeypatch.setitem(sys.modules, "sender_resolver",
                            SimpleNamespace(sender_intel=fake_intel))
        monitor.db = FakeDB({})
        got = await resolver._resolve_sender_username(monitor, data, NavReport())
        assert got is None  # السلوك المجمد: مرساة tg:// تبقى كما هي

    async def test_event_username_invalid_falls_through(self, resolver, monitor, monkeypatch):
        """username فاسد من الحدث لا يُعتمد — يجرب الكاش ثم DB."""
        data = _base_data()
        data["sender_username"] = "9999"  # أرقام فقط = ليس username حقيقياً
        fake_intel = FakeResolverIntel({777000111: FakeUser(777000111, "good_one")})
        monkeypatch.setitem(sys.modules, "sender_resolver",
                            SimpleNamespace(sender_intel=fake_intel))
        got = await resolver._resolve_sender_username(monitor, data, NavReport())
        assert got == "good_one"


# ═══════════════════════════════════════════════════════════════════════════
# 3) سلّم المجموعة/الرسالة — C1..C4 + M1..M4
# ═══════════════════════════════════════════════════════════════════════════
class TestChatLadder:
    async def test_c1_entity_username(self, resolver, monitor):
        """مجموعة عامة: كيان باسم مستخدم → روابط t.me/u للجميع."""
        data = _base_data(chat_id=-1001234567890, message_id=789)
        chat_info = {"entity": FakeChannel(-1001234567890, "eng_group"),
                     "title": "هندسة", "group_link": "#", "msg_link": "#"}
        report = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info, None, None, report)
        assert chat_info["group_link"] == "https://t.me/eng_group"
        assert chat_info["msg_link"] == "https://t.me/eng_group/789"
        assert chat_info["username"] == "eng_group"
        assert report.message_strategy == "public_message"

    async def test_c2_event_username_when_entity_failed(self, resolver, monitor):
        """فشل حل الكيان لكن الحدث يحمل username → يُستخدم + العنوان من الحدث."""
        data = _base_data(chat_id=-1001234567890, message_id=10)
        data["chat_title"] = "مجموعة الطلاب"
        chat_info = {"entity": None, "title": None, "group_link": "#", "msg_link": "#"}
        report = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info, "students_g", None, report)
        assert chat_info["group_link"] == "https://t.me/students_g"
        assert chat_info["msg_link"] == "https://t.me/students_g/10"
        assert chat_info["title"] == "مجموعة الطلاب"
        assert report.chat_strategy == "event_username"

    async def test_c3_private_supergroup_c_form(self, resolver, monitor, monkeypatch):
        """مجموعة فائقة خاصة: صيغة c/‏-100 القانونية من المعرف الحقيقي."""
        _restore = _nav_export_enabled(False)  # عزل عن الشبكة
        try:
            data = _base_data(chat_id=-1001234567890, message_id=456)
            chat_info = {"entity": FakeChannel(-1001234567890, None),
                         "title": "خاص", "group_link": "#", "msg_link": "#"}
            report = NavReport()
            await resolver._resolve_chat_links(monitor, data, chat_info, None, None, report)
            assert chat_info["group_link"] == "https://t.me/c/1234567890"
            assert chat_info["msg_link"] == "https://t.me/c/1234567890/456"
        finally:
            _restore()

    async def test_basic_group_honestly_unlinkable(self, resolver, monitor):
        """مجموعة أساسية (بلا username ومعرفها ليس -100) — تيليجرام نفسها
        لا توفّر روابط رسائل لها: النتيجة الصادقة «#» لا رابط مُختلق."""
        data = _base_data(chat_id=-1234567, message_id=42)
        chat_info = {"entity": FakeBasicChat(-1234567), "title": "قديم",
                     "group_link": "#", "msg_link": "#"}
        report = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info, None, None, report)
        assert chat_info["group_link"] == "#"
        assert chat_info["msg_link"] == "#"
        assert report.chat_strategy == "unlinkable"

    async def test_c4_export_discovers_public_username(self, resolver, monitor):
        """آخر ملذ: exportMessageLink يكتشف username عام → يرقّي روابط c/
        المخصوصة بالأعضاء إلى روابط تُفتح للجميع (الموثوقية أولاً)."""
        client = ExportClient(link="https://t.me/discovered_group/999")
        data = _base_data(chat_id=-1001234567890, message_id=456)
        chat_info = {"entity": FakeChannel(-1001234567890, None), "title": "خ",
                     "group_link": "https://t.me/c/1234567890",
                     "msg_link": "https://t.me/c/1234567890/456"}
        report = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info, None, client, report)
        assert chat_info["group_link"] == "https://t.me/discovered_group"
        assert chat_info["msg_link"] == "https://t.me/discovered_group/456"  # id الحالي
        assert chat_info["username"] == "discovered_group"
        assert "export" in report.chat_strategy

    async def test_export_cache_reused_without_new_call(self, resolver, monitor):
        client = ExportClient(link="https://t.me/discovered_group/999")
        data = _base_data(chat_id=-100555000111, message_id=1)
        chat_info = {"entity": FakeChannel(-100555000111, None), "title": "خ",
                     "group_link": "https://t.me/c/555000111",
                     "msg_link": "https://t.me/c/555000111/1"}
        report = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info, None, client, report)
        assert len(client.calls) == 1
        # تنبيه ثانٍ لنفس المحادثة برسالة مختلفة — من الكاش، برسالة الصحيحة
        data2 = _base_data(chat_id=-100555000111, message_id=2)
        chat_info2 = {"entity": FakeChannel(-100555000111, None), "title": "خ",
                      "group_link": "https://t.me/c/555000111",
                      "msg_link": "https://t.me/c/555000111/2"}
        await resolver._resolve_chat_links(monitor, data2, chat_info2, None, client, NavReport())
        assert len(client.calls) == 1  # لا استدعاء ثانٍ
        assert chat_info2["msg_link"] == "https://t.me/discovered_group/2"  # بمعرف الرسالة الحالية

    async def test_export_failure_negative_cache(self, resolver, monitor):
        client = ExportClient(fail=True)
        data = _base_data(chat_id=-100777000333, message_id=7)
        chat_info = {"entity": FakeChannel(-100777000333, None), "title": "خ",
                     "group_link": "https://t.me/c/777000333",
                     "msg_link": "https://t.me/c/777000333/7"}
        report = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info, None, client, report)
        assert len(client.calls) == 1
        # فشل أول لا يوقف السلسلة — الروابط المبنية بقيت، والرابط c/ صالح
        assert chat_info["group_link"] == "https://t.me/c/777000333"
        # محاولة ثانية مباشرة — الكاش السلبي يمنع التكرار (لا مطرقة على API)
        chat_info2 = dict(chat_info)
        report2 = NavReport()
        await resolver._resolve_chat_links(monitor, data, chat_info2, None, client, report2)
        assert len(client.calls) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 4) بوابة التحقق النهائية — لا يصل للسلك أي رابط غير قابل للعمل
# ═══════════════════════════════════════════════════════════════════════════
class TestVerifyGate:
    def test_strips_malformed_links_to_frozen_fallback(self, resolver):
        chat_info = {"group_link": "https://t.me/BAD NAME!", "msg_link": "https://t.me/c/-5/1"}
        report = resolver.verify_navigation(chat_info, "good_user", 5)
        assert chat_info["group_link"] == "#"   # «الرابط غير متاح» المجمدة
        assert chat_info["msg_link"] == "#"
        assert report.verified_ok is False

    def test_keeps_valid_links(self, resolver):
        chat_info = {"group_link": "https://t.me/mygroup", "msg_link": "https://t.me/c/1234567890/7"}
        report = resolver.verify_navigation(chat_info, "ahmed_99", 5)
        assert chat_info["group_link"] == "https://t.me/mygroup"
        assert chat_info["msg_link"] == "https://t.me/c/1234567890/7"
        assert report.verified_ok is True

    def test_hash_separator_never_treated_as_link(self, resolver):
        chat_info = {"group_link": "#", "msg_link": "#"}
        report = resolver.verify_navigation(chat_info, None, 0)
        assert chat_info["group_link"] == "#" and chat_info["msg_link"] == "#"
        assert report.verified_ok is True  # "#" الحالة الطبيعية غير الموصوفة


# ═══════════════════════════════════════════════════════════════════════════
# 5) نقطة الدخول — فشل أي استراتيجية لا يوقف البقية + سيناريو محادثة منعولة
# ═══════════════════════════════════════════════════════════════════════════
class TestEntryPoint:
    async def test_inaccessible_chat_still_resolves_sender(self, resolver, monitor, monkeypatch):
        """محادثة منعولة (الكيان None، لا username، export يفشل) — حل المرسل
        يستمر ويُنتج username من DB، والتحقق يُنهي السلسلة دون انفجار."""
        client = ExportClient(fail=True)
        data = _base_data(chat_id=-100666000444, message_id=3)
        data["sender_username"] = None
        fake_intel = FakeResolverIntel({})
        monkeypatch.setitem(sys.modules, "sender_resolver",
                            SimpleNamespace(sender_intel=fake_intel))
        monitor.db = FakeDB({"username": "survivor_user"})
        chat_info = {"entity": None, "title": None, "group_link": "#", "msg_link": "#"}
        got = await resolver.enrich_alert_navigation(
            monitor=monitor, data=data, chat_info=chat_info,
            event_chat_username=None, send_client=client,
        )
        assert got == "survivor_user"
        assert chat_info["group_link"] == "https://t.me/c/666000444"  # من المعرف الحقيقي
        assert chat_info["msg_link"] == "https://t.me/c/666000444/3"

    async def test_total_resolver_failure_is_fail_safe(self, monitor):
        """انفجار مرحلة المرسل كله → لا يوقف سلّم المجموعة، والتحقق يُنهي
        السلسلة — عزل الاستراتيجيات (مطلب: فشل واحدة لا يوقف البقية)."""
        data = _base_data()
        chat_info = {"entity": None, "title": None, "group_link": "#", "msg_link": "#"}
        class Boom:
            async def get_sender_contact(self, *a, **k):
                raise RuntimeError("db down")
        monitor.db = Boom()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(singleton, "_resolve_sender_username",
                       lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
            got = await singleton.enrich_alert_navigation(
                monitor=monitor, data=data, chat_info=chat_info,
                event_chat_username=None, send_client=None,
            )
        assert got is None                       # المرسل فشل بلا انفجار خارجي
        # سلّم المجموعة استمر: صيغة c/ القانونية من المعرف الحقيقي
        assert chat_info["group_link"] == "https://t.me/c/1234567890"
        assert chat_info["msg_link"] == "https://t.me/c/1234567890/55"


# ═══════════════════════════════════════════════════════════════════════════
# 6) تكامل البنّاء المتجمّد — الأزرار والتنبيه من بيانات المُحلّل
# ═══════════════════════════════════════════════════════════════════════════
class TestFrozenBuilderIntegration:
    def test_basic_group_button_omitted_not_dead_link(self):
        """إصلاح بناء المعرف: مجموعة أساسية كانت تُنتج t.me/c/-1234567/…
        الميتة — الآن الزر يُحذف بصدق (لا أزرار مكسورة)."""
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": -1234567, "message_id": 42, "username": None},
        )
        urls = [s for row in rows or [] for s in _all_specs(row)]
        assert all(not u.startswith("https://t.me/c/") for u in urls)

    def test_minus100_button_still_canonical(self):
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": -1001234567890, "message_id": 456, "username": None},
        )
        urls = [s for row in rows or [] for s in _all_specs(row)]
        assert "https://t.me/c/1234567890/456" in urls

    async def test_alert_from_enriched_chat_info(self, monitor):
        """التنبيه النهائي من بيانات مُحلّى: رابط المرسل + بطاقة «نص
        الرسالة:» الموحدة (v10.9) — والأزرار من بوت التنبيهات فقط.

        v10.9: الروابط انتقلت من سطر «رابط الرسالة :» إلى زر «عرض
        الرسالة»؛ النص بطاقة موحدة (blockquote) لكل التنبيهات."""
        sender = {"id": 777000111, "display": "أحمد", "username": "ahmed_99", "access_hash": None}
        chat = {"entity": None, "title": "مجموعة الطلاب", "id": -1001234567890,
                "message_id": 789, "username": "mygroup",
                "group_link": "https://t.me/mygroup", "msg_link": "https://t.me/mygroup/789"}
        alert, buttons = monitor._build_alert(sender, chat, "واجب", "نص التنبيه", {"msg_hash": "h1"})
        assert '<a href="https://t.me/ahmed_99">@ahmed_99</a>' in alert
        # القالب الموحد v10.9: البطاقة تُظهر النص الأصلي كاملاً
        assert '<b>💬 الرسالة:</b>\n<blockquote>نص التنبيه</blockquote>' in alert
        assert "رابط الرسالة" not in alert  # الروابط في الأزرار الآن
        assert buttons is None  # الأزرار من بوت التنبيهات فقط

    async def test_no_username_uses_recovered_db_username(self, monitor):
        """مرسل بلا username في الحدث لكن له اسم محفوظ → الرابط t.me بدل
        مرساة tg:// الميتة (الإجراء #1 يعمل فعلياً)."""
        sender = {"id": 777000111, "display": "سارة", "username": "recovered_user", "access_hash": None}
        chat = {"entity": None, "title": None, "id": -1001234567890,
                "message_id": 456, "username": None,
                "group_link": "https://t.me/c/1234567890", "msg_link": "https://t.me/c/1234567890/456"}
        alert, _ = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "h2"})
        assert '<a href="https://t.me/recovered_user">@recovered_user</a>' in alert


# ═══════════════════════════════════════════════════════════════════════════
# 7) قناة الإثراء في خط المعالجة — username مفقود → استعلام DB
# ═══════════════════════════════════════════════════════════════════════════
class TestPipelineEnrichment:
    async def test_missing_username_triggers_db_fallback(self, monitor):
        """حدث بمرسل موجود لكن بلا username (كيان min) — الاستعلام يجلب
        الاسم المحفوظ من رسالة سابقة، فيصبح رابط t.me عالمياً."""
        data = _base_data()
        data["sender_username"] = None
        data["sender_first_name"] = "م"
        data["_sender_needs_enrichment"] = False
        fake_db = FakeDB({"username": "persisted_user", "access_hash": 123})
        monitor.db = fake_db
        # نستدعِ دالة الإثراء مباشرة بنفس شرط خط المعالجة الجديد
        if data.get("sender_id") and not data.get("sender_username"):
            await monitor._enrich_sender_from_db(data)
        assert data["sender_username"] == "persisted_user"
        assert fake_db.calls == 1

    async def test_existing_username_never_queried(self, monitor):
        data = _base_data()
        data["sender_username"] = "already_here"
        fake_db = FakeDB({"username": "other"})
        monitor.db = fake_db
        if data.get("sender_id") and not data.get("sender_username"):
            await monitor._enrich_sender_from_db(data)
        assert fake_db.calls == 0  # لا استعلام — لا تغيير للسلوك الحالي


# ═══════════════════════════════════════════════════════════════════════════
# 8) القافلة الكاملة عبر نقطة الدخول — سيناريوهات المواصفة الثمانية
# ═══════════════════════════════════════════════════════════════════════════
class TestSpecScenarios:
    async def _run(self, resolver, monitor, *, chat_id, message_id, entity,
                   event_chat_username, sender_username, db_contact=None,
                   send_client=None):
        data = _base_data(chat_id=chat_id, message_id=message_id)
        data["sender_username"] = sender_username
        monitor.db = FakeDB(db_contact or {})
        chat_info = {"entity": entity, "title": getattr(entity, "title", None),
                     "group_link": "#", "msg_link": "#"}
        uname = await resolver.enrich_alert_navigation(
            monitor=monitor, data=data, chat_info=chat_info,
            event_chat_username=event_chat_username, send_client=send_client,
        )
        return data, chat_info, uname

    async def test_public_group(self, resolver, monitor):
        _restore = _nav_export_enabled(False)
        try:
            _, chat, uname = await self._run(
                resolver, monitor, chat_id=-1001234567890, message_id=10,
                entity=FakeChannel(-1001234567890, "pub_g"), event_chat_username=None,
                sender_username="sender_u",
            )
            assert chat["group_link"] == "https://t.me/pub_g"
            assert chat["msg_link"] == "https://t.me/pub_g/10"
            assert uname == "sender_u"
        finally:
            _restore()

    async def test_private_supergroup(self, resolver, monitor):
        _restore = _nav_export_enabled(False)
        try:
            _, chat, uname = await self._run(
                resolver, monitor, chat_id=-100111222333, message_id=20,
                entity=FakeChannel(-100111222333, None), event_chat_username=None,
                sender_username=None, db_contact={"username": "db_u"},
            )
            assert chat["group_link"] == "https://t.me/c/111222333"
            assert chat["msg_link"] == "https://t.me/c/111222333/20"
            assert uname == "db_u"  # المرسل من DB رغم فشل الحدث
        finally:
            _restore()

    async def test_channel(self, resolver, monitor):
        _restore = _nav_export_enabled(False)
        try:
            _, chat, _ = await self._run(
                resolver, monitor, chat_id=-100999888777, message_id=30,
                entity=FakeChannel(-100999888777, "news_channel"), event_chat_username=None,
                sender_username=None,
            )
            assert chat["msg_link"] == "https://t.me/news_channel/30"
        finally:
            _restore()

    async def test_missing_access_hash_keeps_id_anchor(self, resolver, monitor):
        """access_hash مفقود → لا openmessage — مرساة tg://user?id= كما هي."""
        _restore = _nav_export_enabled(False)
        try:
            data, chat, uname = await self._run(
                resolver, monitor, chat_id=-1001234567890, message_id=40,
                entity=FakeChannel(-1001234567890, "g"), event_chat_username=None,
                sender_username=None,
            )
            assert uname is None
            # البنّاء المتجمّد يحافظ على سلوكه: بلا username وبلا hash → tg://user
            sender = {"id": 777000111, "display": "خالد", "username": None, "access_hash": None}
            alert, _ = monitor._build_alert(sender, chat, "ك", "ن", {"msg_hash": "h3"})
            assert 'href="tg://user?id=777000111"' in alert
        finally:
            _restore()

    async def test_failed_primary_strategy_chain_continues(self, resolver, monitor):
        """فشل الاستراتيجية الأولى (كيان None بلا username في الحدث) ثم
        نجاح التالية (username الحدث) — السلسلة لا تتوقف."""
        data, chat, uname = await self._run(
            resolver, monitor, chat_id=-1001234567890, message_id=50,
            entity=None, event_chat_username="second_g",
            sender_username="sender_u",
        )
        assert chat["group_link"] == "https://t.me/second_g"
        assert chat["msg_link"] == "https://t.me/second_g/50"
        assert uname == "sender_u"


def _nav_export_enabled(flag: bool):
    """CFG frozen — نفس نمط test_buttons: تعديل كائني + استعادة مؤكدة."""
    old = getattr(CFG, "NAV_EXPORT_LINK_ENABLED", True)
    object.__setattr__(CFG, "NAV_EXPORT_LINK_ENABLED", flag)
    def _restore():
        object.__setattr__(CFG, "NAV_EXPORT_LINK_ENABLED", old)
    return _restore


def _all_specs(row):
    """(text, url) specs — لفحص وجود الروابط في الصف."""
    out = []
    for b in row:
        t = getattr(b, "type", None)
        if t is not None and hasattr(t, "url") and getattr(t, "url", None):
            out.append(t.url)
        elif getattr(b, "url", None):
            out.append(b.url)
        else:
            out.append("")
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 9) v10.6 — S4 discovery: users.getUsers عبر رسالة المصدر
# ═══════════════════════════════════════════════════════════════════════════
import sender_resolver as _real_sender_resolver  # noqa: E402
from nav_resolver import harden_sender_anchor  # noqa: E402
from telethon.errors import FloodWaitError  # noqa: E402


class DiscoveryUser:
    """مشابه لكيان User الذي يعيده users.getUsers."""

    def __init__(self, user_id: int, username: Optional[str],
                 access_hash: int = 555, deleted: bool = False):
        self.id = user_id
        self.username = username
        self.access_hash = access_hash
        self.deleted = deleted


class DiscoveryClient:
    """عميل وهمي يدعم get_input_entity + users.getUsers فقط."""

    def __init__(self, users: Optional[List[Any]] = None, fail: bool = False,
                 exc: Optional[Exception] = None):
        self.users = users or []
        self.fail = fail
        self.exc = exc
        self.calls = 0

    async def get_input_entity(self, key: Any):
        return FakeChannel(int(key), username=None)

    async def __call__(self, req: Any):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.fail:
            raise RuntimeError("USER_ID_INVALID")
        return SimpleNamespace(users=self.users)


class FakeHashStore:
    def __init__(self):
        self.records: List[Any] = []

    def record(self, account_name, sender_id, access_hash):
        self.records.append((account_name, sender_id, access_hash))
        return True


class FakeUpsertDB:
    def __init__(self, contact: Optional[Dict[str, Any]] = None):
        self.contact = contact or {}
        self.upserts: List[Dict[str, Any]] = []

    async def get_sender_contact(self, sender_id: int):
        return self.contact or None

    async def upsert_sender_contact(self, sender_data: Dict[str, Any]):
        self.upserts.append(dict(sender_data))


async def _noop_persist_hash(sender_id: int, access_hash: int) -> None:
    return None


def _discovery_monitor(client: Any, db: Any = None) -> SimpleNamespace:
    """مراقب خفيف — الواجهة الدنيا التي يستخدمها مسار S4 فقط."""
    return SimpleNamespace(
        client=client,
        db=db if db is not None else FakeUpsertDB(),
        _account_name_for_client=lambda c: "NavAcc",
        _persist_account_hash=_noop_persist_hash,
    )


def _patch_sender_resolver(monkeypatch, hash_store: FakeHashStore):
    """استبدل وحدة sender_resolver عند الاستيراد داخل nav_resolver فقط —
        الدوال الحقيقية تبقى، والمخزن يصبح وهمياً."""
    async def _noop_hash(sender_id: int, access_hash: int) -> None:
        return None

    monkeypatch.setitem(sys.modules, "sender_resolver", SimpleNamespace(
        input_user_from_message_context=_real_sender_resolver.input_user_from_message_context,
        sender_url_forms=_real_sender_resolver.sender_url_forms,
        account_hash_store=hash_store,
    ))


class TestSenderDiscoveryS4:
    async def test_discovery_recovers_hidden_username(self, resolver, monkeypatch):
        """مرسل بلا username في الحدث/الكاش/DB — يُكتشف عبر رسالته المصدر
        (InputUserFromMessage) ويعود باسم صالح يُغذّي التنبيه فوراً."""
        store = FakeHashStore()
        _patch_sender_resolver(monkeypatch, store)
        client = DiscoveryClient(users=[DiscoveryUser(777000111, "found_via_msg")])
        db = FakeUpsertDB()
        mon = _discovery_monitor(client, db)
        data = _base_data()
        data["sender_username"] = None
        report = NavReport()
        got = await resolver._resolve_sender_username(mon, data, report, send_client=None)
        assert got == "found_via_msg"
        assert data["sender_username"] == "found_via_msg"       # COALESCE في بيانات الحدث
        assert report.sender_strategy == "discovered_via_message"
        assert client.calls == 1                                 # مكالمة واحدة فقط
        assert db.upserts == [{"sender_id": 777000111, "username": "found_via_msg"}]
        assert store.records == [("NavAcc", 777000111, 555)]     # hash لكل حساب — ذاكرة mention

    async def test_discovery_deleted_user_honest_miss(self, resolver, monkeypatch):
        """حساب محذوف: لا يوجد أي رابط شرعي — miss صادق + كاش سلبي."""
        store = FakeHashStore()
        _patch_sender_resolver(monkeypatch, store)
        client = DiscoveryClient(users=[DiscoveryUser(777000111, None, deleted=True)])
        mon = _discovery_monitor(client)
        data = _base_data()
        data["sender_username"] = None
        report = NavReport()
        got = await resolver._resolve_sender_username(mon, data, report, send_client=None)
        assert got is None
        assert client.calls == 1
        # المكالمة الثانية تُخدَم من الكاش السلبي — بلا RPC جديد
        got2 = await resolver._resolve_sender_username(mon, data, NavReport(), send_client=None)
        assert got2 is None
        assert client.calls == 1

    async def test_discovery_username_absent_by_privacy(self, resolver, monkeypatch):
        """المستخدم الذي أخفى username يرجع بلا username من تيليجرام نفسه —
            نتعامل بصدق (miss) ولا نخترع شيئاً."""
        store = FakeHashStore()
        _patch_sender_resolver(monkeypatch, store)
        client = DiscoveryClient(users=[DiscoveryUser(777000111, None)])
        mon = _discovery_monitor(client)
        data = _base_data()
        data["sender_username"] = None
        got = await resolver._resolve_sender_username(mon, data, NavReport(), send_client=None)
        assert got is None

    async def test_discovery_floodwait_honored(self, resolver, monkeypatch):
        """FloodWait يُحترم: miss فوري + كاش سلبي (بلا أي إعادة محاولة)."""
        store = FakeHashStore()
        _patch_sender_resolver(monkeypatch, store)
        client = DiscoveryClient(exc=FloodWaitError(request=None, capture=9))
        mon = _discovery_monitor(client)
        data = _base_data()
        data["sender_username"] = None
        report = NavReport()
        got = await resolver._resolve_sender_username(mon, data, report, send_client=None)
        assert got is None
        assert client.calls == 1
        assert any(n.startswith("discovery:flood") for n in report.notes)

    async def test_discovery_chain_continues_to_second_client(self, resolver, monkeypatch):
        """فشل العميل الأول لا يوقف السلسلة — العميل الثاني ينجح."""
        store = FakeHashStore()
        _patch_sender_resolver(monkeypatch, store)
        bad = DiscoveryClient(fail=True)
        good = DiscoveryClient(users=[DiscoveryUser(777000111, "second_view")])
        mon = _discovery_monitor(bad, FakeUpsertDB())
        data = _base_data()
        data["sender_username"] = None
        got = await resolver._resolve_sender_username(mon, data, NavReport(), send_client=good)
        assert got == "second_view"
        assert bad.calls == 1 and good.calls == 1

    async def test_discovery_disabled_flag_skips_network(self, resolver, monkeypatch):
        restore = None
        old = getattr(CFG, "NAV_USER_DISCOVERY_ENABLED", True)
        object.__setattr__(CFG, "NAV_USER_DISCOVERY_ENABLED", False)

        def _restore():
            object.__setattr__(CFG, "NAV_USER_DISCOVERY_ENABLED", old)
        restore = _restore
        try:
            store = FakeHashStore()
            _patch_sender_resolver(monkeypatch, store)
            client = DiscoveryClient(users=[DiscoveryUser(777000111, "never_asked")])
            mon = _discovery_monitor(client)
            data = _base_data()
            data["sender_username"] = None
            got = await resolver._resolve_sender_username(mon, data, NavReport(), send_client=None)
            assert got is None
            assert client.calls == 0  # المفتاح مُغلق → صفر استدعاءات شبكة
        finally:
            if restore:
                restore()

    async def test_discovery_no_clients_fail_safe(self, resolver):
        """لا عملاء متاحين → None بلا أي انهيار (فشل-آمن حرفياً)."""
        mon = _discovery_monitor(None)
        data = _base_data()
        data["sender_username"] = None
        got = await resolver._resolve_sender_username(mon, data, NavReport(), send_client=None)
        assert got is None

    async def test_server_answer_without_our_user_never_guesses(self, resolver, monkeypatch):
        """إجابة من الخادم لا تحتوي مرسلنا → تُهمَل ولا يُخترع اسم منها."""
        store = FakeHashStore()
        _patch_sender_resolver(monkeypatch, store)
        client = DiscoveryClient(users=[DiscoveryUser(999999999, "someone_else")])
        mon = _discovery_monitor(client)
        data = _base_data()
        data["sender_username"] = None
        got = await resolver._resolve_sender_username(mon, data, NavReport(), send_client=None)
        assert got is None
        assert client.calls == 1  # محاولة واحدة فشلت منطقياً → فشل كلي بلا اختراع


# ═══════════════════════════════════════════════════════════════════════════
# 10) v10.6 — harden_sender_anchor: ضمان قابلية النقر في المسار الاحتياطي
# ═══════════════════════════════════════════════════════════════════════════
class TestHardenSenderAnchor:
    def test_replaces_tg_user_anchor(self):
        html = '👤: <a href="tg://user?id=777000111">أحمد</a>'
        out = harden_sender_anchor(html, 777000111, "https://t.me/c/1234567890/456")
        assert out == '👤: <a href="https://t.me/c/1234567890/456">أحمد</a>'

    def test_replaces_openmessage_anchor(self):
        html = '👤: <a href="tg://openmessage?user_id=777000111">أحمد</a>'
        out = harden_sender_anchor(html, 777000111, "https://t.me/mygroup/789")
        assert out == '👤: <a href="https://t.me/mygroup/789">أحمد</a>'

    def test_username_anchor_never_touched(self):
        html = '👤: <a href="https://t.me/ahmed_99">أحمد</a>'
        assert harden_sender_anchor(html, 777000111, "https://t.me/c/1/2") == html

    def test_invalid_fallback_ignored(self):
        html = '👤: <a href="tg://user?id=5">م</a>'
        for bad in (None, "", "#", "http://t.me/g/1", "javascript:alert(1)"):
            assert harden_sender_anchor(html, 5, bad) == html

    def test_displayed_text_byte_identical(self):
        """العقد المتجمد: فقط href يتغير — النص الظاهر حرفياً كما هو."""
        html = '<b>الرسالة:</b>\nنص\n\n👤: <a href="tg://user?id=42">سارة</a>'
        out = harden_sender_anchor(html, 42, "https://t.me/c/123/9")
        assert out.replace('href="https://t.me/c/123/9"', 'href="tg://user?id=42"') == html

    def test_integration_build_alert_no_username(self, monitor):
        """تكامل مع البانِر المجمد: تنبيه بلا username + رابط رسالة صالح →
            المرساة النهائية تقود للرسالة المصدر بدل المرساة الميتة."""
        sender = {"id": 777000111, "display": "سارة", "username": None, "access_hash": None}
        chat = {"entity": None, "title": "ق", "id": -1001234567890, "message_id": 456,
                "username": None, "group_link": "https://t.me/c/1234567890",
                "msg_link": "https://t.me/c/1234567890/456"}
        alert, _ = monitor._build_alert(sender, chat, "ك", "نص", {"msg_hash": "h3"})
        assert 'href="tg://user?id=777000111"' in alert          # الشكل المجمد قبل التصلب
        hardened = harden_sender_anchor(alert, 777000111, chat["msg_link"])
        assert 'href="https://t.me/c/1234567890/456"' in hardened
        assert "سارة" in hardened


# ═══════════════════════════════════════════════════════════════════════════
# 11) v10.6 — سلّم زر «مراسلة»: كل نقرة تصل لمكان حقيقي
# ═══════════════════════════════════════════════════════════════════════════
class TestContactButtonLadder:
    def test_username_still_wins(self):
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": "ahmed_99"},
            chat={"id": -1001234567890, "message_id": 42, "username": None},
        )
        assert "https://t.me/ahmed_99" in [u for row in rows or [] for u in _all_specs(row)]

    def test_no_username_falls_back_to_source_message(self):
        """بلا username: زر مراسلة يفتح الرسالة المصدر (HTTPS يعمل على كل
            العملاء) — اللمس على صورة المرسل يفتح ملفه الشخصي دائماً."""
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": -1001234567890, "message_id": 42, "username": None},
        )
        urls = [u for row in rows or [] for u in _all_specs(row)]
        assert "https://t.me/c/1234567890/42" in urls  # مراسلة = رابط الرسالة

    def test_basic_group_keeps_openmessage_last_resort(self):
        """مجموعة أساسية (لا روابط رسائل في تيليجرام) → الشكل القديم
            tg://openmessage يبقى آخر خيار متاح بصدق."""
        rows = build_dynamic_buttons(
            sender={"id": 5, "username": None},
            chat={"id": -1234567, "message_id": 42, "username": None},
        )
        urls = [u for row in rows or [] for u in _all_specs(row)]
        assert "tg://openmessage?user_id=5" in urls
        assert all(not u.startswith("https://t.me/c/") for u in urls)

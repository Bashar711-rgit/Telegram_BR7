"""v9.36 — استبدال المراقب القديم المعطوب في add_runtime_monitor.

السبب الجذري الموثق من الإنتاج (2026-09-27): الحسابان 1 و2 سجّلا الدخول
بنجاح (الجلسة حُفظت في DB + Render env) لكن add_runtime_monitor كان يجد
مراقب الإقلاع القديم (المُنشأ بلا جلسة) فيعيد «already + خطأ No session
القديم» دون أي اتصال بالجلسة الجديدة — فلا يُفعَّل الحساب ولا يراقب،
وفشل زر «توصيل» من اللوحة بنفس الرسالة القديمة المهجورة.

التغطية:
- مراقب حي فعلاً يُحترم (already + ok) دون إعادة بناء.
- مراقب قديم غير متصل يُقطع اتصاله ويُزال ويُبنى بجلسة طازجة.
- فشل قطع اتصال المراقب القديم لا يمنع الاستبدال (فشل-آمن).
- فشل اتصال المراقب الجديد يُظهر الخطأ الحقيقي الجديد (لا القديم).
- المراقب الجديد الفاشل يبقى في القائمة ثم يُستبدل في المحاولة التالية
  (حلقة شفاء ذاتي).
- ترقية main_client عند أول اتصال ناجح (سلوك الإقلاع نفسه).
"""

import pytest

import main
from main import EnhancedTelegramBot


class _FakeClient:
    def __init__(self, alive: bool = True):
        self._alive = alive

    def is_connected(self) -> bool:
        return self._alive


class _FakeMonitor:
    """مراقب مصغّر بنفس الواجهة التي يعتمدها add_runtime_monitor."""

    instances: list = []
    fail_next: bool = False

    def __init__(self, account, db=None, flt=None, main_client=None):
        self.account = dict(account or {})
        self.is_connected = False
        self.client = None
        self._last_connect_error = None
        self.connect_calls = 0
        self.disconnected = False
        self.bot = None
        _FakeMonitor.instances.append(self)

    def set_bot(self, bot):
        self.bot = bot

    async def connect(self) -> bool:
        self.connect_calls += 1
        if _FakeMonitor.fail_next:
            self._last_connect_error = "سبب حقيقي جديد من تيليجرام"
            return False
        self.is_connected = True
        self.client = _FakeClient(alive=True)
        return True

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False

    @staticmethod
    def _is_client_alive(client) -> bool:
        try:
            attr = getattr(client, "is_connected", None)
            return bool(attr() if callable(attr) else attr)
        except Exception:
            return False


class _BrokenDisconnectMonitor(_FakeMonitor):
    async def disconnect(self):
        raise RuntimeError("disconnect boom")


def _bot() -> EnhancedTelegramBot:
    bot = EnhancedTelegramBot.__new__(EnhancedTelegramBot)
    bot.db = None
    bot.filter = None
    bot.main_client = None
    bot.monitors = []
    return bot


def _stale(prefix: str = "ACCOUNT_1") -> _FakeMonitor:
    m = _FakeMonitor({"prefix": prefix, "name": prefix})
    m.is_connected = False
    m.client = None
    m._last_connect_error = "No session - login via /login"
    return m


@pytest.fixture(autouse=True)
def _patch_monitor(monkeypatch):
    _FakeMonitor.instances = []
    _FakeMonitor.fail_next = False
    monkeypatch.setattr(main, "EnhancedAccountMonitor", _FakeMonitor)
    yield
    _FakeMonitor.instances = []
    _FakeMonitor.fail_next = False


class TestStaleMonitorReplacement:
    @pytest.mark.asyncio
    async def test_stale_monitor_replaced_with_fresh_session(self):
        """السيناريو الفعلي من الإنتاج: مراقب إقلاع بلا جلسة + جلسة طازجة
        بعد تسجيل دخول ناجح — يجب أن يُستبدل المراقب لا رفض الاتصال."""
        bot = _bot()
        stale = _stale("ACCOUNT_1")
        bot.monitors = [stale]
        res = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_1", "name": "acc", "session_string": "NEW-SESSION"}
        )
        assert res == {"ok": True}
        assert stale.disconnected is True
        assert stale not in bot.monitors
        fresh = _FakeMonitor.instances[-1]
        assert fresh in bot.monitors
        assert fresh.account["session_string"] == "NEW-SESSION"
        assert fresh.connect_calls == 1
        assert fresh.is_connected is True

    @pytest.mark.asyncio
    async def test_alive_monitor_respected_no_rebuild(self):
        """مراقب حي فعلاً = already + ok دون إعادة بناء (لا انقطاع بلا داعٍ)."""
        bot = _bot()
        live = _FakeMonitor({"prefix": "ACCOUNT_2", "name": "acc2"})
        live.is_connected = True
        live.client = _FakeClient(alive=True)
        bot.monitors = [live]
        _FakeMonitor.instances.clear()  # نظّف سجل الإنشاء قبل الاستدعاء
        res = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_2", "name": "acc2", "session_string": "X"}
        )
        assert res == {"ok": True, "already": True}
        assert bot.monitors == [live]
        assert _FakeMonitor.instances == []  # لم يُبنَ مراقب جديد

    @pytest.mark.asyncio
    async def test_stale_disconnect_failure_still_replaces(self):
        """فشل قطع اتصال المراقب القديم لا يمنع الاستبدال (فشل-آمن)."""
        bot = _bot()
        stale = _BrokenDisconnectMonitor({"prefix": "ACCOUNT_3", "name": "acc3"})
        stale.is_connected = False
        stale.client = None
        stale._last_connect_error = "No session - login via /login"
        bot.monitors = [stale]
        res = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_3", "name": "acc3", "session_string": "S3"}
        )
        assert res == {"ok": True}
        assert stale not in bot.monitors
        assert _FakeMonitor.instances[-1].account["session_string"] == "S3"

    @pytest.mark.asyncio
    async def test_connect_failure_surfaces_new_error_not_stale(self):
        """فشل الاتصال الجديد يُظهر سببه الحقيقي الجديد — لا «No session»
        القديم المهجور الذي كان يُخفي الخطأ الفعلي."""
        bot = _bot()
        bot.monitors = [_stale("ACCOUNT_4")]
        _FakeMonitor.fail_next = True
        res = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_4", "name": "acc4", "session_string": "S4"}
        )
        assert res["ok"] is False
        assert res["error"] == "سبب حقيقي جديد من تيليجرام"
        assert "No session" not in res["error"]

    @pytest.mark.asyncio
    async def test_failed_new_monitor_self_heals_on_next_add(self):
        """المراقب الجديد الفاشل يبقى في القائمة ثم يُستبدل تلقائياً في
        المحاولة التالية (شفاء ذاتي بلا إعادة تشغيل)."""
        bot = _bot()
        bot.monitors = [_stale("ACCOUNT_5")]
        _FakeMonitor.fail_next = True
        first = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_5", "name": "acc5", "session_string": "S5"}
        )
        assert first["ok"] is False
        assert len(bot.monitors) == 1  # المراقب الجديد الفاشل حلّ محل القديم
        failed = _FakeMonitor.instances[-1]
        assert failed.connect_calls == 1

        _FakeMonitor.fail_next = False
        second = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_5", "name": "acc5", "session_string": "S5"}
        )
        assert second == {"ok": True}
        assert failed not in bot.monitors
        assert bot.monitors[0].is_connected is True

    @pytest.mark.asyncio
    async def test_main_client_promoted_on_first_success(self):
        """أول اتصال ناجح يرقّي عميله ليكون main_client (سلوك الإقلاع)."""
        bot = _bot()
        bot.monitors = [_stale("ACCOUNT_6")]
        res = await bot.add_runtime_monitor(
            {"prefix": "ACCOUNT_6", "name": "acc6", "session_string": "S6"}
        )
        assert res == {"ok": True}
        assert bot.main_client is bot.monitors[0].client

    @pytest.mark.asyncio
    async def test_missing_prefix_rejected(self):
        bot = _bot()
        res = await bot.add_runtime_monitor({"name": "بلا بادئة"})
        assert res == {"ok": False, "error": "prefix مفقود"}
        assert bot.monitors == []

#!/usr/bin/env python3
"""اختبارات الرد التعويضي F — v11.5 (مواصفة المستخدم §11/§17).

كل التفاعلات mocked — لا شبكة ولا رسائل حقيقية. يُغطى:
  * حدود الساعة لكل user_id + الحد اليومي + التعطيل الكلي.
  * تعطيل الوجهة broadcast.
  * SKIP: لا user_id / لا access_hash / لا عميل / بث / كتابة ممنوعة /
    FloodWait / SlowMode.
  * OK مع تحقق الكيان MessageEntityMentionName في الرسالة المرسلة.
  * FAIL عندما يصل الرد بلا الكيان (لا ادعاء نجاح).
  * عزل hash: الكيان يُبنى بـInputUser(user_id, access_hash) للحساب
    المالك نفسه (لا hash حساب في سياق آخر).
  * الفاصل العشوائي 2–5 ث (حدوده).
  * حجز الحصة قبل الإرسال — الخطأ غير المتوقع يستهلك محاولة (منع التكرار).
"""

import asyncio
from typing import Any, Dict, List

import pytest

import reply_fix as rf
from reply_fix import ReplyFixer, get_reply_fixer, reset_reply_fixer

UID = 5601276336
HASH = 7234567890123
OTHER_HASH = 999888777111
CHAT = -1001234567890
ALERT_MSG_ID = 555


class _FakeEntities(list):
    pass


class _SentMessage:
    def __init__(self, entities: List[Any]) -> None:
        self.id = 424242
        self.entities = entities


class MessageEntityMentionName:
    """بديل مبسط — الاسم المطابق لفحص type(e).__name__ في الوحدة."""

    def __init__(self, offset: int = 0, length: int = 2) -> None:
        self.offset = offset
        self.length = length


class _InputUserStub:
    def __init__(self, user_id: int, access_hash: int) -> None:
        self.user_id = user_id
        self.access_hash = access_hash


class _EntityStub:
    """بديل InputMessageEntityMentionName يحفظ InputUser المُمرر."""

    def __init__(self, offset: int, length: int, user_id: Any) -> None:
        self.offset = offset
        self.length = length
        self.user_id = user_id


class _FakeClient:
    """عميل تيليجرام وهمي — يسجل النداءات ويتحكم بالنتيجة/الأخطاء."""

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


@pytest.fixture()
def fast_fixer():
    """F بفاصل اختباري صغير (0.001–0.002) كي لا تُبطئ الاختبارات —
    حدود الـjitter الافتراضية (2–5) تُختبر في اختبار مستقل."""
    return ReplyFixer(jitter_min=0.001, jitter_max=0.002)


@pytest.fixture()
def patch_telethon(monkeypatch):
    """حقن بدائل Telethon المستقرة داخل وحدة reply_fix (بلا شبكة)."""
    monkeypatch.setattr(rf, "InputMessageEntityMentionName", _EntityStub)
    monkeypatch.setattr(rf, "InputUser", _InputUserStub)
    monkeypatch.setattr(rf, "_TELETHON_TYPES", True)
    monkeypatch.setattr(rf, "_REPLY_LEN_UTF16", 2)


class TestCaps:
    def test_hourly_cap_blocks_second_attempt(self):
        f = ReplyFixer(hourly_per_user=1, jitter_min=0, jitter_max=0)
        ok, why = f.allowed(UID)
        assert ok is True
        f._record_attempt(UID)
        ok, why = f.allowed(UID)
        assert ok is False and why == "hourly_cap"

    def test_daily_cap(self):
        f = ReplyFixer(daily_cap=2, jitter_min=0, jitter_max=0)
        f._record_attempt(1)
        f._record_attempt(1)
        ok, why = f.allowed(2)  # الحد اليومي كلي — ليس لكل مستخدم
        assert ok is False and why == "daily_cap"

    def test_disabled_globally(self):
        f = ReplyFixer(enabled=False)
        ok, why = f.allowed(UID)
        assert ok is False and why == "disabled"

    def test_broadcast_disable(self):
        f = ReplyFixer(jitter_min=0, jitter_max=0)
        f.disable_for_broadcast("target is broadcast channel")
        ok, why = f.allowed(UID)
        assert ok is False and why == "broadcast_target"


class TestSkips:
    @pytest.mark.asyncio
    async def test_skip_no_user_id(self, fast_fixer, patch_telethon):
        res = await fast_fixer.run(_FakeClient(), chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=0, access_hash=HASH)
        assert res.startswith("SKIP:no_user_id")

    @pytest.mark.asyncio
    async def test_skip_no_access_hash(self, fast_fixer, patch_telethon):
        res = await fast_fixer.run(_FakeClient(), chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=None)
        assert res.startswith("SKIP:no_access_hash")

    @pytest.mark.asyncio
    async def test_skip_no_client(self, fast_fixer, patch_telethon):
        res = await fast_fixer.run(None, chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=HASH)
        assert res.startswith("SKIP:no_client")

    @pytest.mark.asyncio
    async def test_skip_write_forbidden(self, fast_fixer, patch_telethon, monkeypatch):
        monkeypatch.setattr(rf, "_TELETHON_ERRORS", True)
        monkeypatch.setattr(rf, "ChatWriteForbiddenError", RuntimeError)
        client = _FakeClient(error=RuntimeError("forbidden"))
        res = await fast_fixer.run(client, chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=HASH)
        assert res.startswith("SKIP:write_forbidden_or_slowmode")

    @pytest.mark.asyncio
    async def test_skip_floodwait(self, fast_fixer, patch_telethon, monkeypatch):
        class _Flood(Exception):
            seconds = 17

        monkeypatch.setattr(rf, "_TELETHON_ERRORS", True)
        monkeypatch.setattr(rf, "FloodWaitError", _Flood)
        client = _FakeClient(error=_Flood("flood"))
        res = await fast_fixer.run(client, chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=HASH)
        assert res == "SKIP:floodwait_17"


class TestSuccessAndVerification:
    @pytest.mark.asyncio
    async def test_ok_when_mention_entity_delivered(self, fast_fixer, patch_telethon):
        client = _FakeClient(entities=[MessageEntityMentionName(offset=0, length=2)])
        res = await fast_fixer.run(client, chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=HASH)
        assert res == "OK"
        call = client.calls[0]
        # الرد مرتبط بالتنبيه reply_to + نصه إشارة واحدة
        assert call["reply_to"] == ALERT_MSG_ID
        assert call["text"] == "👤"

    @pytest.mark.asyncio
    async def test_fail_when_entity_missing(self, fast_fixer, patch_telethon):
        client = _FakeClient(entities=[])  # الرد وصل بلا الكيان
        res = await fast_fixer.run(client, chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=HASH)
        assert res.startswith("FAIL:entity_missing")

    @pytest.mark.asyncio
    async def test_hash_isolated_to_owner_account(self, fast_fixer, patch_telethon):
        """مواصفة §11.4/§5.2: InputUser بhash حساب المراقبة المالك حصراً —
        لا يُعاد استخدام hash حساب آخر (عزل الحسابات)."""
        client = _FakeClient(entities=[MessageEntityMentionName()])
        await fast_fixer.run(client, chat_id=CHAT,
                             reply_to_msg_id=ALERT_MSG_ID,
                             user_id=UID, access_hash=HASH)
        entity = client.calls[0]["entities"][0]
        assert entity.user_id.user_id == UID
        assert entity.user_id.access_hash == HASH
        assert entity.user_id.access_hash != OTHER_HASH

    @pytest.mark.asyncio
    async def test_second_run_within_hour_skipped(self, fast_fixer, patch_telethon):
        """مواصفة §11: لا تكرار F للمستخدم نفسه داخل الساعة — حتى عبر
        إعادة معالجة التنبيه."""
        client = _FakeClient(entities=[MessageEntityMentionName()])
        first = await fast_fixer.run(client, chat_id=CHAT,
                                     reply_to_msg_id=ALERT_MSG_ID,
                                     user_id=UID, access_hash=HASH)
        assert first == "OK"
        second = await fast_fixer.run(client, chat_id=CHAT,
                                      reply_to_msg_id=ALERT_MSG_ID,
                                      user_id=UID, access_hash=HASH)
        assert second.startswith("SKIP:hourly_cap")
        assert len(client.calls) == 1  # لم يُرسل شيئاً ثانياً

    @pytest.mark.asyncio
    async def test_unexpected_error_consumes_attempt(self, fast_fixer, patch_telethon, monkeypatch):
        """خطأ غير متوقع: الحجز تم — يمنع تكرار F (مقاومة التكرار §11)."""
        class _Safe(Exception):
            pass

        class _Unexpected(Exception):
            pass

        monkeypatch.setattr(rf, "_TELETHON_ERRORS", True)
        monkeypatch.setattr(rf, "ChatWriteForbiddenError", _Safe)
        monkeypatch.setattr(rf, "FloodWaitError", _Safe)
        monkeypatch.setattr(rf, "SlowModeWaitError", _Safe)
        client = _FakeClient(error=_Unexpected("boom"))
        res = await fast_fixer.run(client, chat_id=CHAT,
                                   reply_to_msg_id=ALERT_MSG_ID,
                                   user_id=UID, access_hash=HASH)
        assert res.startswith("FAIL:_Unexpected")
        ok, why = fast_fixer.allowed(UID)
        assert ok is False and why == "hourly_cap"


class TestJitterBounds:
    @pytest.mark.asyncio
    async def test_default_jitter_within_2_5_seconds(self, monkeypatch, patch_telethon):
        """مواصفة §11: فاصل عشوائي بين 2 و5 ثوانٍ (القيم الافتراضية)."""
        captured: List[float] = []

        async def _fake_sleep(sec):
            captured.append(sec)

        monkeypatch.setattr(rf.asyncio, "sleep", _fake_sleep)
        f = ReplyFixer()  # الافتراضي 2–5
        client = _FakeClient(entities=[MessageEntityMentionName()])
        await f.run(client, chat_id=CHAT, reply_to_msg_id=ALERT_MSG_ID,
                    user_id=UID, access_hash=HASH)
        assert captured and 2.0 <= captured[0] <= 5.0, captured


class TestSingleton:
    def test_get_reply_fixer_returns_same_instance(self):
        reset_reply_fixer()
        a = get_reply_fixer()
        b = get_reply_fixer()
        assert a is b
        reset_reply_fixer()

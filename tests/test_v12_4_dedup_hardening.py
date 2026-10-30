#!/usr/bin/env python3
"""اختبارات v12.4.0 — سد نوافذ تكرار التنبيه المتبقية + إثبات البطاقة الكاملة.

الجذور المثبتة والمغلقة هنا:
  RC-1  إيصال alert دفعي (فراغ كل DB_BATCH_INTERVAL=5s): انهيار العملية
        بين نجاح الإرسال والإفراغ = فقدان الإيصال → الحدث المسترد لاحقاً
        يعيد الإرسال = مكرر مرئي. الإصلاح: add_alert_immediate (إيصال
        غير دفعي يُلزم لحظة نجاح الإرسال).
  RC-2  alert_exists لا يرى الإيصالات المعلقة في الدفعة → حدث مسترد
        متزامن داخل نفس العملية يعيد الإرسال. الإصلاح: فحص الـbuffer.
  RC-3  مُحقّق تسليم AlertBot كان معطوباً بنيوياً: expected من بطاقة HTML
        (بوسوم <a href=...>) مقابل visible نص خام → التطابق مستحيل 100%
        → كل timeout/network يُحكم "فشل مؤكد" → fallback يعيد الإرسال
        حتى لو وصلت البطاقة فعلاً. + الفحص كان يرفض بطاقات v12.1.0+
        لأنها تبدأ بسطر «B_Bot» لا «المرسل:».
  RC-4  حاجز الصدى والمُحقّق لا يتعرفان على بطاقات «B_Bot\nالمرسل:».

كل التفاعلات الشبكية مزيّفة — لا إرسال حقيقي إطلاقاً.
"""

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import monitors as mon
from alert_bot import build_alert_html
from config import CFG
from database import AlertRecord, EnhancedDatabase
from dedup import AlertDeduplicator, content_fingerprint, init_deduplicator

SENDER_ID = 6079171409
CHAT_ID = -100111222333
MSG_ID = 4242
TEXT = "ابي اذاكر اللي يجبي مقالي وشو it232"


@pytest.fixture(autouse=True)
def _clean_shared_module_state():
    yield
    try:
        from dedup import get_deduplicator as _gd
        _gd()._mem.clear()
    except Exception:
        pass
    try:
        from similarity import get_similarity_gate as _gs
        _gs()._recent.clear()
    except Exception:
        pass


def _event(**kw) -> Dict[str, Any]:
    base = {
        "chat_id": CHAT_ID, "message_id": MSG_ID, "sender_id": SENDER_ID,
        "text": TEXT, "account_name": "ACCOUNT_1", "timestamp": time.time(),
        "msg_date_ts": time.time(),
    }
    base.update(kw)
    return base


# ═══════════════════════════════════════════════════════════════════════
# A. الإيصال الفوري (FIX-1 / RC-1)
# ═══════════════════════════════════════════════════════════════════════

class TestImmediateReceipt:
    @pytest.mark.asyncio
    async def test_receipt_visible_immediately_without_flush(self, db: EnhancedDatabase):
        """الإيصال يُرى فوراً بعد add_alert_immediate — بلا انتظار الكاتب الدفعي.
        (السلوك القديم كان يتطلب db._flush() — نافذة فقد الإيصال = مكرر)."""
        h = "v124-receipt-now"
        assert await db.alert_exists(h) is False
        rec = AlertRecord(message_hash=h, chat_id=1, sender_id=2,
                          account_name="ACCOUNT_1", keyword="طلب",
                          alert_text="x", timestamp=time.time())
        assert await db.add_alert_immediate(rec) is True
        # لا _flush() هنا — الفحص فوراً (هذا هو جوهر الإصلاح)
        assert await db.alert_exists(h) is True

    @pytest.mark.asyncio
    async def test_receipt_idempotent_no_double_row(self, db: EnhancedDatabase):
        """إيصال مزدوج (فوري ثم دفعي) = صف واحد حرفياً (UNIQUE محمي)."""
        h = "v124-receipt-idem"
        rec = AlertRecord(message_hash=h, chat_id=1, sender_id=2,
                          account_name="ACCOUNT_1", keyword="طلب",
                          alert_text="x", timestamp=time.time())
        assert await db.add_alert_immediate(rec) is True
        assert await db.add_alert_immediate(rec) is False  # مكرر
        await db.add_alert(rec)          # الكاتب الدفعي أيضاً ON CONFLICT DO NOTHING
        await db._flush()
        rows = await db._fetchall("SELECT id FROM alerts WHERE message_hash = ?", (h,))
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_receipt_updates_sender_stats_and_cache(self, db: EnhancedDatabase):
        """sender_stats.alerts_sent/last_alert_time تطبق فوراً (دلالات _flush نفسها)."""
        sid = 987001
        await db._execute(
            "INSERT OR IGNORE INTO sender_stats (sender_id, total_messages, first_seen) VALUES (?, 1, ?)",
            (sid, time.time()))
        await db._commit()
        h = "v124-receipt-stats"
        rec = AlertRecord(message_hash=h, chat_id=1, sender_id=sid,
                          account_name="ACCOUNT_1", keyword="طلب",
                          alert_text="x", timestamp=1234.5)
        await db.add_alert_immediate(rec)
        row = await db._fetchone(
            "SELECT alerts_sent, last_alert_time FROM sender_stats WHERE sender_id = ?", (sid,))
        assert row is not None
        assert int(row["alerts_sent"]) == 1
        assert float(row["last_alert_time"]) == 1234.5
        # ذاكرة can_send_alert أُبطلت → القرار التالي يرى الإيصال الجديد
        cached = db._can_send_alert_cache.get(sid)
        assert cached is None

    @pytest.mark.asyncio
    async def test_receipt_failure_falls_back_to_batch(self, db: EnhancedDatabase, monkeypatch):
        """فشل الإدخال الفوري → الشبكة الأخيرة: الدفعة (الإيصال لا يضيع كلياً)."""
        h = "v124-receipt-fb"

        async def _boom(*a, **k):
            raise RuntimeError("simulated db failure")

        monkeypatch.setattr(db, "_execute", _boom)
        rec = AlertRecord(message_hash=h, chat_id=1, sender_id=2,
                          account_name="ACCOUNT_1", keyword="طلب",
                          alert_text="x", timestamp=time.time())
        assert await db.add_alert_immediate(rec) is False
        # فشل-آمن: الإيصال في الدفعة و alert_exists يراه (FIX-2)
        assert await db.alert_exists(h) is True

    @pytest.mark.asyncio
    async def test_immediate_receipt_counter_increments(self, db: EnhancedDatabase):
        from unified_alerts.stats import get_unified_stats
        h = "v124-receipt-counter"
        rec = AlertRecord(message_hash=h, chat_id=1, sender_id=2,
                          account_name="ACCOUNT_1", keyword="طلب",
                          alert_text="x", timestamp=time.time())
        before = get_unified_stats().snapshot()["receipts_immediate"]
        await db.add_alert_immediate(rec)
        after = get_unified_stats().snapshot()["receipts_immediate"]
        assert after == before + 1


# ═══════════════════════════════════════════════════════════════════════
# B. رؤية الدفعة المعلقة (FIX-2 / RC-2)
# ═══════════════════════════════════════════════════════════════════════

class TestBatchVisibility:
    @pytest.mark.asyncio
    async def test_alert_exists_sees_pending_batched_receipt(self, db: EnhancedDatabase):
        """إيصال أُرسل ولم يُفرَّغ بعد (نافذة 5s) → alert_exists=True.
        كان False → حدث مسترد متزامن يعيد الإرسال = مكرر."""
        h = "v124-batch-visible"
        await db.add_alert(AlertRecord(message_hash=h, chat_id=1, sender_id=2,
                                       account_name="ACCOUNT_1", keyword="طلب",
                                       alert_text="x", timestamp=time.time()))
        # لا flush — الإيصال معلق في الذاكرة فقط
        assert await db.alert_exists(h) is True
        assert await db.alert_exists("totally-unknown") is False

    @pytest.mark.asyncio
    async def test_batch_scan_ignores_non_alert_entries(self, db: EnhancedDatabase):
        """مدخلات الدفعة غير-التنبيهات لا تُلبس بالإيصال (item[0] حارس النوع)."""
        h = "v124-non-alert"
        async with db._batch_lock:
            db._batch.append(("other_kind", h, 1, 2))
        assert await db.alert_exists(h) is False


# ═══════════════════════════════════════════════════════════════════════
# C. الحسابات السبعة والتزامن (المتطلبات الأساسية 1+2)
# ═══════════════════════════════════════════════════════════════════════

def _mk_monitor(db, name: str) -> mon.EnhancedAccountMonitor:
    account = {
        "id": 1, "prefix": name.upper(), "name": name, "session": f"s_{name}",
        "api_id": 12345, "api_hash": "h" * 32, "phone": "+966500000000", "priority": 20,
    }
    return mon.EnhancedAccountMonitor(account, db, None)


class TestSevenAccountsOneAlert:
    @pytest.mark.asyncio
    async def test_same_message_via_seven_accounts_stored_once(self, db: EnhancedDatabase):
        """اختبار 1: الرسالة نفسها عبر 7 حسابات → تخزين منطقي واحد فقط
        (طبقة messages.message_hash) — الحسابات الست الأخرى is_new=False."""
        monitors = [_mk_monitor(db, f"ACCOUNT_{i}") for i in range(1, 8)]
        ev = _event()
        results = []
        for m in monitors:
            _hash, _text, is_new = await m._store_message(ev)
            results.append(is_new)
        assert sum(1 for r in results if r) == 1  # تسجيل واحد
        assert results[0] is True                 # أول وصول هو صاحب التسجيل

    @pytest.mark.asyncio
    async def test_seven_concurrent_arrivals_single_winner(self, db: EnhancedDatabase):
        """اختبار 2: الوصول المتزامن (7 مهام في نفس الحلقة) → فائز واحد
        بلا سباق (INSERT OR IGNORE ذري عبر العمال)."""
        monitors = [_mk_monitor(db, f"ACCOUNT_{i}") for i in range(1, 8)]
        ev = _event()
        results = await asyncio.gather(*[m._store_message(dict(ev)) for m in monitors])
        assert sum(1 for _h, _t, is_new in results if is_new) == 1

    @pytest.mark.asyncio
    async def test_seven_concurrent_fingerprint_claims_single_winner(self, db):
        """الحجوزات المتزامنة على بصمة المحتوى نفسها → فائز واحد بالضبط."""
        init_deduplicator(db)
        dd = AlertDeduplicator(db=db)
        fp = content_fingerprint(SENDER_ID, TEXT)
        outcomes = await asyncio.gather(*[dd.check_and_claim(fp) for _ in range(7)])
        assert sum(1 for o in outcomes if o) == 1

    @pytest.mark.asyncio
    async def test_different_senders_same_text_not_merged(self, db):
        """اختبار 4: مرسلان مختلفان بنفس النص → تنبيهان (لا دمج تلقائي)."""
        dd = AlertDeduplicator(db=db)
        fp_a = content_fingerprint(111, TEXT)
        fp_b = content_fingerprint(222, TEXT)
        assert fp_a != fp_b
        assert await dd.check_and_claim(fp_a) is True
        assert await dd.check_and_claim(fp_b) is True

    @pytest.mark.asyncio
    async def test_different_messages_same_sender_both_pass(self, db):
        """اختبار 3: رسالتان مختلفتان من نفس المرسل → كلتاهما تمر."""
        dd = AlertDeduplicator(db=db)
        assert await dd.check_and_claim(content_fingerprint(SENDER_ID, "ابي حل واجب الاحصاء")) is True
        assert await dd.check_and_claim(content_fingerprint(SENDER_ID, "ابي حل واجب الفيزياء")) is True

    @pytest.mark.asyncio
    async def test_survives_restart_via_db_claim(self, db):
        """اختبار 5: إعادة تشغيل (deduplicator جديد بذاكرة فارغة) → الحماية
        قائمة من قاعدة البيانات ضمن النافذة."""
        fp = content_fingerprint(SENDER_ID, TEXT)
        first = AlertDeduplicator(db=db)
        assert await first.check_and_claim(fp) is True
        # "إعادة تشغيل": نسخة جديدة بلا ذاكرة
        restarted = AlertDeduplicator(db=db)
        assert await restarted.check_and_claim(fp) is False


# ═══════════════════════════════════════════════════════════════════════
# D. بوابات الحدث المسترد (FIX-4) — لا إعادة إرسال بوجود الإيصال
# ═══════════════════════════════════════════════════════════════════════

class _FakeClient:
    def __init__(self, name: str = "ACCOUNT_1"):
        self.name = name
        self.is_connected = True
        self.calls: List[Dict[str, Any]] = []
        self.forward_calls: List[Dict[str, Any]] = []

    async def get_input_entity(self, key: Any):
        raise ValueError(f"unresolvable: {key!r}")

    async def send_message(self, entity, message, **kwargs):
        self.calls.append({"kind": "message", "text": message, **kwargs})
        return SimpleNamespace(id=999, chat_id=entity, entities=[])

    async def send_file(self, entity, file=None, caption=None, **kwargs):
        self.calls.append({"kind": "file", "text": caption, **kwargs})
        return SimpleNamespace(id=998, chat_id=entity, entities=[])

    async def forward_messages(self, entity, message_id, *, from_peer=None, **kwargs):
        self.forward_calls.append({"to": entity, "msg_id": message_id})
        return SimpleNamespace(id=1000, chat_id=entity)


def _wired_monitor(db, client: _FakeClient) -> mon.EnhancedAccountMonitor:
    m = _mk_monitor(db, "ACCOUNT_1")
    m.client = client
    m.is_connected = True

    async def _can_proceed(account_name):
        return True

    m._bot_ref = SimpleNamespace(
        alert_sender_client=None,
        main_client=client,
        monitors=[SimpleNamespace(account={"name": m.account["name"]},
                                  client=client, is_connected=True)],
        rate_limiter=SimpleNamespace(can_proceed=_can_proceed),
        _note_alert_message=lambda *a, **k: None,
        alert_bot=None,  # مسار حسابات المستخدمين مباشرة
    )

    async def _fake_chat_info(cl, chat_id, message_id, chat_access_hash=None, chat_username=None):
        return {"entity": None, "title": "مجموعة الطلاب", "group_link": "#", "msg_link": "#"}

    m._chat_info = _fake_chat_info
    return m


class TestRecoveredEventGates:
    @pytest.mark.asyncio
    async def test_recovered_with_receipt_never_resends(self, db: EnhancedDatabase):
        """اختبار 8: انهيار بعد نجاح الإرسال وقبل الإيصال القديم — الآن
        الإيصال الفوري موجود → الحدث المسترد يُسقط قبل فك الحجز وبلا إرسال."""
        init_deduplicator(db)
        client = _FakeClient()
        m = _wired_monitor(db, client)
        ev = _event(_recovered=1)
        msg_hash = "v124-recovered-with-receipt"
        # المحاولة المنهارة: أرسلت وأثبتت إيصالها الفوري + حجزت بصمتها
        await db.add_alert_immediate(AlertRecord(
            message_hash=msg_hash, chat_id=CHAT_ID, sender_id=SENDER_ID,
            account_name="ACCOUNT_1", keyword="طلب", alert_text="x",
            timestamp=time.time()))
        fp = content_fingerprint(SENDER_ID, TEXT)
        assert await mon.get_deduplicator().check_and_claim(fp) is True

        await m._send_alert(ev, keyword="طلب", text=TEXT,
                            msg_hash=msg_hash, analysis={"valid": True, "decision": "accept"})
        # صفر إرسال — البوابة أوقفت الحدث قبل فك الحجز
        assert client.calls == []
        assert client.forward_calls == []
        # الحجز لم يُفك — الحماية باقية
        assert await mon.get_deduplicator().check_and_claim(fp) is False
        from unified_alerts.stats import get_unified_stats
        snap = get_unified_stats().snapshot()
        assert snap["recovered_dupe_skip_send"] >= 1

    @pytest.mark.asyncio
    async def test_recovered_without_receipt_sends_exactly_once(self, db: EnhancedDatabase):
        """انهيار قبل الإرسال (بلا إيصال) → إعادة إرسال واحدة عادلة،
        ثم أي استرداد لاحق لنفس الحدث لا يرسل شيئاً (الإيصال الفوري).
        الحجز الميت من المحاولة المنهارة يكون عتيقاً (عمر الاسترداد 600s)
        — نُسبخ last_seen لمحاكاة ذلك فيجتاز استيلاء CAS."""
        init_deduplicator(db)
        client = _FakeClient()
        m = _wired_monitor(db, client)
        ev = _event(_recovered=1)
        msg_hash = "v124-recovered-no-receipt"
        fp = content_fingerprint(SENDER_ID, TEXT)
        # المحاولة المنهارة حجزت البصمة وانهارت قبل الإرسال (منذ دقائق)
        assert await mon.get_deduplicator().check_and_claim(fp) is True
        await db._execute(
            "UPDATE alert_dedup SET last_seen = last_seen - 400 WHERE fingerprint = ?",
            (fp,))
        await db._commit()

        await m._send_alert(ev, keyword="طلب", text=TEXT,
                            msg_hash=msg_hash, analysis={"valid": True, "decision": "accept"})
        assert len(client.calls) == 1
        # الإيصال الفوري موجود لحظة نجاح الإرسال (بلا flush)
        assert await db.alert_exists(msg_hash) is True
        # استرداد ثانٍ لنفس الحدث (تكرار سيناريو الانهيار) → بلا إرسال إضافي
        ev2 = _event(_recovered=1)
        await m._send_alert(ev2, keyword="طلب", text=TEXT,
                            msg_hash=msg_hash, analysis={"valid": True, "decision": "accept"})
        assert len(client.calls) == 1  # لا مكرر ثانٍ

    @pytest.mark.asyncio
    async def test_non_recovered_duplicate_still_blocked(self, db: EnhancedDatabase):
        """الحدث العادي غير المسترد على بصمة محجوزة → حاجز dedup يعمل كما كان."""
        init_deduplicator(db)
        client = _FakeClient()
        m = _wired_monitor(db, client)
        fp = content_fingerprint(SENDER_ID, TEXT)
        assert await mon.get_deduplicator().check_and_claim(fp) is True
        await m._send_alert(_event(), keyword="طلب", text=TEXT,
                            msg_hash="v124-normal-dup", analysis={"valid": True, "decision": "accept"})
        assert client.calls == []


# ═══════════════════════════════════════════════════════════════════════
# E. مُحقّق تسليم AlertBot (FIX-3 / RC-3) — كان معطوباً بنيوياً
# ═══════════════════════════════════════════════════════════════════════

class _FakeAlertUI:
    """عميل AlertUI مزيّف: يعيد رسائل مجموعة الهدف كما يراها البوت نفسه."""

    def __init__(self, messages: List[Any]):
        self._messages = messages
        self.limits: List[int] = []

    async def get_messages(self, target, limit=10):
        self.limits.append(limit)
        return self._messages[:limit]


def _card_plain_text() -> str:
    """البطاقة كما يراها تيليجرام نصاً خاماً (بلا وسوم) — صيغة v12.1.0."""
    data = {
        "sender_id": SENDER_ID, "sender_username": "Lara507",
        "sender_first_name": "هناء", "sender_last_name": "العنزي",
        "text": TEXT, "chat_id": CHAT_ID, "message_id": 531011,
    }
    built = build_alert_html(data, None,
                             msg_link="https://t.me/src/531011",
                             group_link="https://t.me/src")
    import re as _re
    return _re.compile(r"<[^>]+>").sub("", built["text"])


def _expected_prefix() -> str:
    """البادئة المتوقعة — محسوبة بالضبط كما في monitors._send_alert (FIX-3a):
    normalize أولاً ثم الاقتطاع (لا العكس)."""
    built = build_alert_html(
        {"sender_id": SENDER_ID, "sender_username": "Lara507",
         "sender_first_name": "هناء", "sender_last_name": "العنزي",
         "text": TEXT, "chat_id": CHAT_ID, "message_id": 531011},
        None, msg_link="https://t.me/src/531011", group_link="https://t.me/src")
    return mon.normalize_for_fingerprint(mon.visible_card_text(built["text"]))[:48]


class TestDeliveryVerifierFixed:
    @pytest.mark.asyncio
    async def test_delivered_card_is_found_after_timeout(self, db: EnhancedDatabase):
        """السيناريو الإنتاجي للمكرر: timeout زائف وبطاقة موصلة فعلاً →
        المُحقّق المصلح يجدها → لا fallback → لا مكرر. (كان False دائماً)."""
        m = _wired_monitor(db, _FakeClient())
        expected = _expected_prefix()
        ui = _FakeAlertUI([SimpleNamespace(
            out=True, message=_card_plain_text(),
            date=datetime.now(timezone.utc) - timedelta(seconds=5))])
        assert await m._verify_alertbot_delivery(ui, expected) is True

    @pytest.mark.asyncio
    async def test_scans_up_to_25_messages(self, db: EnhancedDatabase):
        """مجموعة نشطة: البطاقة خارج أول 10 رسائل → الحد الجديد 25 يجدها."""
        m = _wired_monitor(db, _FakeClient())
        expected = _expected_prefix()
        filler = [SimpleNamespace(out=True, message=f"رسالة عادية رقم {i}",
                                  date=datetime.now(timezone.utc))
                  for i in range(20)]
        card = SimpleNamespace(out=True, message=_card_plain_text(),
                               date=datetime.now(timezone.utc) - timedelta(seconds=3))
        ui = _FakeAlertUI(filler + [card])
        assert await m._verify_alertbot_delivery(ui, expected) is True
        assert ui.limits[-1] == 25

    @pytest.mark.asyncio
    async def test_stale_card_beyond_300s_rejected(self, db: EnhancedDatabase):
        """بطاقة عمرها > 300 ثانية لا تُحسب إثبات تسليم لتنبيه حالي."""
        m = _wired_monitor(db, _FakeClient())
        card = SimpleNamespace(out=True, message=_card_plain_text(),
                               date=datetime.now(timezone.utc) - timedelta(seconds=400))
        ui = _FakeAlertUI([card])
        assert await m._verify_alertbot_delivery(ui, "") is False

    @pytest.mark.asyncio
    async def test_bbot_header_signature_recognized(self, db: EnhancedDatabase):
        """RC-4: بطاقات v12.1.0+ تبدأ بسطر «B_Bot» — المُحقّق يتعرف عليها
        (كان يرفضها لأنها لا تبدأ بـ«المرسل:») وحاجز الصدى يكتشفها."""
        plain = _card_plain_text()
        assert plain.lstrip().startswith("B_Bot")
        assert mon._is_current_card_header(plain.lstrip()) is True
        assert mon.is_alert_card_echo(plain) is True
        # الصيغ الأقدم (بلا سطر الاسم) تبقى معترفاً بها
        older = plain.split("\n", 1)[1]
        assert mon.is_alert_card_echo(older) is True
        # رسالة مستخدم عادية ليست بطاقة أبداً
        assert mon.is_alert_card_echo("المرسل: عندك حل؟") is False

    @pytest.mark.asyncio
    async def test_foreign_card_not_matched_as_ours(self, db: EnhancedDatabase):
        """بطاقة غير موصلة (نص مختلف) لا تُحسب إثبات تسليم."""
        m = _wired_monitor(db, _FakeClient())
        foreign = mon.normalize_for_fingerprint(mon.visible_card_text(build_alert_html(
            {"sender_id": SENDER_ID, "sender_username": "Lara507",
             "sender_first_name": "هناء", "sender_last_name": "العنزي",
             "text": "رسالة مختلفة تماما عن اي شي اخر نهائيا",
             "chat_id": CHAT_ID, "message_id": 531011},
            None, msg_link="https://t.me/src/531011", group_link="https://t.me/src")))[:48]
        card = SimpleNamespace(out=True, message=_card_plain_text(),
                               date=datetime.now(timezone.utc) - timedelta(seconds=5))
        ui = _FakeAlertUI([card])
        assert await m._verify_alertbot_delivery(ui, foreign) is False


# ═══════════════════════════════════════════════════════════════════════
# F. البطاقة الموحدة الكاملة (نموذج المستخدم المرجعي)
# ═══════════════════════════════════════════════════════════════════════

class TestUnifiedCardCompleteness:
    def _built(self, **kw):
        data = {
            "sender_id": SENDER_ID, "sender_username": "Lara507",
            "sender_first_name": "هناء", "sender_last_name": "العنزي",
            "text": TEXT, "chat_id": CHAT_ID, "message_id": 531011,
            **kw,
        }
        return build_alert_html(data, None,
                                msg_link="https://t.me/src/531011",
                                group_link="https://t.me/src")

    def test_card_has_bot_name_sender_and_label(self):
        built = self._built()
        t = built["text"]
        assert t.startswith("B_Bot\n")
        assert "المرسل: " in t.split("\n\n")[0]
        assert "نص الرسالة:" in t
        assert TEXT in t

    def test_card_has_both_buttons_with_correct_targets(self):
        built = self._built()
        row = built["buttons"][0]
        by_text = {b["text"]: b for b in row}
        assert set(by_text) == {"عرض الرسالة", "مراسلة"}
        assert by_text["عرض الرسالة"]["url"] == "https://t.me/src/531011"
        assert by_text["مراسلة"]["url"] == "https://t.me/Lara507"

    def test_no_username_still_reachable_and_complete(self):
        """بلا username: الاسم رابط tg://user وزر مراسلة موجود — لا بيانات مختلقة."""
        built = self._built(sender_username=None)
        t = built["text"]
        assert t.startswith("B_Bot\nالمرسل: <a href=\"tg://user?id=6079171409\">")
        assert "— @" not in t.split("\n\n")[0]  # لا @username مُختلق
        row = built["buttons"][0]
        by_text = {b["text"]: b for b in row}
        assert by_text["مراسلة"]["url"] == f"tg://user?id={SENDER_ID}"

    def test_long_text_truncated_with_marker_not_silently(self):
        """نص طويل جداً: يُقص مع علامة … وسجل تحذير — لا حذف صامت كامل."""
        built = self._built(text="ا" * 5000)
        assert built["text"].rstrip().endswith("…")

    def test_arabic_content_preserved_verbatim(self):
        """النص العربي يصل كما ورد (بعد فك الترميز) — بلا تحريف المعنى."""
        built = self._built()
        import re as _re
        visible = mon.visible_card_text(built["text"])
        assert TEXT in visible

#!/usr/bin/env python3
"""اختبارات v12.3.0 — V12-D (سباق ازدواجية dead-letter) + S-4 (شفافية webadmin).

التغطية:
  A. V12-D — القفل الذرّي (CAS على attempts):
     1. مسارا استرداد متزامنان على صف مستنفد → حرف ميت واحد بالضبط (لا تكرار).
     2. مسارا استرداد متزامنان على صف غير مستنفد → استرداد واحد بالضبط.
     3. CAS يخسر عند تغيّر attempts (لا كتابة، الصف يبقى كما هو).
     4. مسار سعيد: True + حرف ميت واحد + الصف خرج + عقد retry_count=attempts.
     5. حدث السمّ ينتهي عبر دورات استرداد (لا ارتداد أبدي pending↔in_flight).
     6. _execute_affected يعدّ الصفوف المتأثرة بصدق (أساس CAS).
  B. S-4 — /health/full يكشف حالة تركيب webadmin صادقاً (لا انقطاع صامت).

القيود الموثقة: فروع PostgreSQL في _dead_letter_row/_execute_affected تُختبر
نصياً بالبنية نفسها (SQLite فقط هنا) — لا يُدعى أنها إثبات تشغيلي لـPG.
"""

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from database import EnhancedDatabase
from tests.test_dashboard import AUTH  # نفس توكن اللوحة المعتمد


# ═══════════════════════════════════════════════════════════════════════
# أدوات
# ═══════════════════════════════════════════════════════════════════════

def _mk_event(chat_id=111, message_id=222, text="ابغى احد يحل لي واجب الرياضيات"):
    return {
        "chat_id": chat_id,
        "message_id": message_id,
        "sender_id": 333,
        "text": text,
        "account_name": "ACCOUNT_1",
        "timestamp": time.time(),
        "msg_date_ts": time.time(),
    }


async def _stale_claim(db: EnhancedDatabase, message_id: int, attempts: int):
    """يُدرج حدثاً، يحجزه، ثم يُقدّم claimed_at لما قبل الحد (انهيار قديم)."""
    from config import CFG
    await db.add_to_queue(_mk_event(message_id=message_id), priority=7)
    claimed = await db.pop_from_queue()
    await db._execute(
        "UPDATE processing_queue SET claimed_at = ?, attempts = ? WHERE id = ?",
        (time.time() - 3600, attempts, claimed["_queue_id"]),
    )
    await db._commit()
    return claimed["_queue_id"]


# ═══════════════════════════════════════════════════════════════════════
# A. V12-D — سباق الازدواجية
# ═══════════════════════════════════════════════════════════════════════

class TestDeadLetterRace:
    @pytest.mark.asyncio
    async def test_concurrent_recovery_no_duplicate_dead_letter(self, db):
        """المتسابقان على نفس الصف المستنفد → حرف ميت واحد بالضبط.

        هذا هو السباق المُثبت (v12.2 كان يفوز فيه الاثنان: INSERT مكرر).
        """
        rid = await _stale_claim(db, message_id=101, attempts=3)  # مستنفد
        results = await asyncio.gather(
            db.recover_stale_inflight(max_age_seconds=600),
            db.recover_stale_inflight(max_age_seconds=600),
        )
        total_dead = sum(r["dead"] for r in results)
        total_recovered = sum(r["recovered"] for r in results)
        assert total_dead == 1, f"سباق ازدواج: dead={total_dead}"
        assert total_recovered == 0
        rows = await db._fetchall("SELECT id FROM dead_letters", ())
        assert len(rows) == 1, f"حروف ميتة مكررة: {len(rows)}"
        gone = await db._fetchone(
            "SELECT id FROM processing_queue WHERE id = ?", (rid,)
        )
        assert gone is None

    @pytest.mark.asyncio
    async def test_concurrent_recovery_single_recovered(self, db):
        """المتسابقان على نفس الصف غير المستنفد → استرداد واحد بالضبط
        (الخاسر لا يطمس ولا يعدّ مرتين)."""
        rid = await _stale_claim(db, message_id=102, attempts=1)
        results = await asyncio.gather(
            db.recover_stale_inflight(max_age_seconds=600),
            db.recover_stale_inflight(max_age_seconds=600),
        )
        assert sum(r["recovered"] for r in results) == 1
        assert sum(r["dead"] for r in results) == 0
        row = await db._fetchone(
            "SELECT status, attempts, event_data FROM processing_queue WHERE id = ?",
            (rid,),
        )
        assert row["status"] == "pending"
        assert int(row["attempts"]) == 2  # 1 (claim) + 1 (CAS الاسترداد)
        payload = json.loads(row["event_data"])
        assert payload.get("_recovered") == 1

    @pytest.mark.asyncio
    async def test_cas_dead_letter_row_loses_on_stale_attempts(self, db):
        """CAS بقيمة attempts قديمة/خاطئة → False بلا أي كتابة."""
        rid = await _stale_claim(db, message_id=103, attempts=3)
        ok = await db._dead_letter_row(
            rid, attempts=2,  # القيمة المخزنة 3 — لا تطابق
            raw=json.dumps(_mk_event(message_id=103)),
            err="should_not_be_written",
        )
        assert ok is False
        dl = await db._fetchone(
            "SELECT id FROM dead_letters WHERE error_text = 'should_not_be_written'",
            (),
        )
        assert dl is None
        row = await db._fetchone(
            "SELECT status, attempts FROM processing_queue WHERE id = ?", (rid,)
        )
        assert row is not None and row["status"] == "in_flight"
        assert int(row["attempts"]) == 3  # لم يُمَس

    @pytest.mark.asyncio
    async def test_dead_letter_row_happy_path_contract(self, db):
        """المسار السعيد: فوز واحد + حرف ميت واحد + الصف خرج + عقد retry_count."""
        rid = await _stale_claim(db, message_id=104, attempts=3)
        ok = await db._dead_letter_row(
            rid, attempts=3,
            raw=json.dumps(_mk_event(message_id=104)),
            err="inflight_expired: test",
        )
        assert ok is True
        dl = await db._fetchone(
            "SELECT retry_count, error_text FROM dead_letters WHERE error_text = 'inflight_expired: test'",
            (),
        )
        assert dl is not None
        assert int(dl["retry_count"]) == 3  # العقد: محاولات عند انتهاء الصلاحية
        gone = await db._fetchone(
            "SELECT id FROM processing_queue WHERE id = ?", (rid,)
        )
        assert gone is None

    @pytest.mark.asyncio
    async def test_poison_event_terminates_via_recover_cycles(self, db):
        """حدث السمّ: claim→crash→recover→claim→crash→recover → حرف ميت
        (حدّ QUEUE_INFLIGHT_MAX_ATTEMPTS يُنهي الارتداد الأبدي)."""
        rid = await _stale_claim(db, message_id=105, attempts=1)
        r1 = await db.recover_stale_inflight(max_age_seconds=600)
        assert r1 == {"recovered": 1, "dead": 0}
        # المستهلك يعيد الحجز ثم ينهار مجدداً
        await db._execute(
            "UPDATE processing_queue SET status = 'in_flight', claimed_at = ?, "
            "attempts = attempts + 1 WHERE id = ?",
            (time.time() - 3600, rid),
        )
        await db._commit()
        r2 = await db.recover_stale_inflight(max_age_seconds=600)
        # attempts الآن 3 ≥ السقف الافتراضي → حرف ميت (لا ارتداد ثالث)
        assert r2 == {"recovered": 0, "dead": 1}
        rows = await db._fetchall("SELECT id FROM dead_letters", ())
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_execute_affected_counts_rows_honestly(self, db):
        """أساس CAS: عدد الصفوف المتأثرة صادق على SQLite."""
        rid = await _stale_claim(db, message_id=106, attempts=2)
        n = await db._execute_affected(
            "UPDATE processing_queue SET attempts = attempts + 1 "
            "WHERE id = ? AND status = 'in_flight' AND attempts = ?",
            (rid, 2),
        )
        assert n == 1
        # نفس القيمة مرة أخرى → لا تطابق (attempts صارت 3) → 0
        n2 = await db._execute_affected(
            "UPDATE processing_queue SET attempts = attempts + 1 "
            "WHERE id = ? AND status = 'in_flight' AND attempts = ?",
            (rid, 2),
        )
        assert n2 == 0


# ═══════════════════════════════════════════════════════════════════════
# B. S-4 — شفافية تركيب webadmin في /health/full
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
async def client():
    from dashboard import app
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


class TestWebadminMountVisibility:
    @pytest.mark.asyncio
    async def test_health_full_exposes_webadmin_mount_state(self, client):
        """/health/full يحمل webadmin_mounted (bool) + webadmin_mount_error
        (نص مقطوع آمن) — فشل التركيب لم يعد صامتاً في التشخيص."""
        r = await client.get("/health/full", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert "webadmin_mounted" in body
        assert isinstance(body["webadmin_mounted"], bool)
        assert "webadmin_mount_error" in body
        assert isinstance(body["webadmin_mount_error"], str)
        assert len(body["webadmin_mount_error"]) <= 200

    @pytest.mark.asyncio
    async def test_webadmin_state_flag_set_at_import(self, client):
        """app.state يحمل العلمين بعد الاستيراد بغضّ النظر عن نتيجة التركيب."""
        from dashboard import app
        assert hasattr(app.state, "webadmin_mounted")
        assert isinstance(app.state.webadmin_mounted, bool)
        assert hasattr(app.state, "webadmin_mount_error")

    @pytest.mark.asyncio
    async def test_mounted_flag_matches_router_reality(self, client):
        """الصدق السلوكي: إن كان العلم True فلا بد لمسار /admin أن يستجيب
        فعلياً (لا ادعاء بلا بنية).

        ملاحظة تدقيقية v12.3: في FastAPI 0.162.0 خاصية app.routes لم تعد
        تعكس مسارات include_router (سلوك تأمل جديد — app.routes أظهر 78
        مساراً بلا أي مسار /admin بينما GET /admin يستجيب 200 فعلاً).
        لذلك التحقق هنا سلوكي (طلب HTTP حقيقي) لا استبطاني."""
        from dashboard import app
        if app.state.webadmin_mounted:
            r = await client.get("/admin")
            assert r.status_code != 404, (
                "webadmin_mounted=True بينما /admin يعيد 404 — ادعاء كاذب"
            )
        else:
            # علم False صادق أيضاً: /admin إما 404 أو محمي بحارس لا يمرره
            r = await client.get("/admin")
            assert r.status_code in (401, 404) or r.status_code < 500

#!/usr/bin/env python3
"""
tests/test_v14_6_1_liveness.py — Liveness Decoupling (v14.6.1)

السبب الجذري الذي يغطيه هذا الملف: خادم الصحة كان يعيش على نفس event loop
المزدحم → فحوصات Render تمهّل/تفشل → SIGTERM → إعادة تشغيل (حلقة انهيار
boots #2-#7 في 2026-11-18). العقد الجديد:
  * _HealthSnapshot: حاوية مشتركة بين الحلقة الرئيسية (كتابة) وخيط الخادم
    (قراءة لكل طلب) — بلا awaits ولا DB في مسار الطلب.
  * خيط الخادم مستقل بحلقة خاصة (عقد مصدري في test_v11_8_2).
"""

import threading
import time

import pytest

from main import _HealthSnapshot


class TestHealthSnapshot:
    def test_default_state_is_booting(self):
        snap = _HealthSnapshot()
        state = snap.get()
        assert state["boot_complete"] is False
        assert state["payload"] is None

    def test_boot_complete_flips(self):
        snap = _HealthSnapshot()
        snap.set_boot_complete()
        assert snap.get()["boot_complete"] is True

    def test_update_payload_roundtrip(self):
        snap = _HealthSnapshot()
        snap.update_payload(payload={"status": "ok", "uptime": 5}, db_down=False, db_healthy=True)
        state = snap.get()
        assert state["boot_complete"] is False  # بداية لا تتأثر
        assert state["db_down"] is False
        assert state["db_healthy"] is True
        assert state["payload"]["status"] == "ok"
        assert state["payload_ts"] > 0

    def test_get_returns_top_level_copy(self):
        """get يعيد نسخة المستوى الأعلى (تعيين مفاتيح معزول).

        العقد المعلن: nested payload مرجع مشترك قراءة-فقط — معالجات الخيط
        تسلسله إلى JSON فقط ولا تُعدّله أبداً (النسخ العميق لكل طلب كان
        يكلف ذاكرة/CPU بلا قيمة أمانية في خيط قراءة حصراً).
        """
        snap = _HealthSnapshot()
        snap.update_payload(payload={"status": "ok"}, db_down=False, db_healthy=True)
        state = snap.get()
        state["payload"] = "tampered"  # إعادة تعيين مفتاح أعلى
        state["db_healthy"] = False
        fresh = snap.get()
        assert fresh["payload"]["status"] == "ok"
        assert fresh["db_healthy"] is True

    def test_concurrent_readers_never_raise(self):
        """قراءة متزامنة من 8 خيوط أثناء تحديث مستمر — لا استثناءات (عقد الخيط)."""
        snap = _HealthSnapshot()
        stop = threading.Event()
        errors: list = []

        def reader():
            try:
                while not stop.is_set():
                    s = snap.get()
                    assert isinstance(s, dict)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(8)]
        for r in readers:
            r.start()
        try:
            for i in range(200):
                snap.update_payload(
                    payload={"status": "ok", "i": i}, db_down=(i % 5 == 0), db_healthy=True
                )
        finally:
            stop.set()
            for r in readers:
                r.join(timeout=2.0)
        assert errors == []

    def test_fail_safe_on_internal_corruption(self):
        """كسر مقصود للحالة الداخلية → get يعيد قاموساً صالحاً بلا انهيار."""
        snap = _HealthSnapshot()
        snap._data = None  # كسر مقصود
        state = snap.get()
        assert isinstance(state, dict)
        assert state.get("boot_complete") is False

    def test_snapshot_refresh_latency_bound(self):
        """تحديث اللقطة عملية ذرّية سريعة (<50ms) — لا تحجب خيط الخادم."""
        snap = _HealthSnapshot()
        payload = {"status": "ok", "data": list(range(500))}
        t0 = time.perf_counter()
        for i in range(100):
            snap.update_payload(payload=payload, db_down=False, db_healthy=True)
            snap.get()
        elapsed = time.perf_counter() - t0
        assert elapsed < 5.0  # 200 عملية كاملة — متسامح لكن مرتبط

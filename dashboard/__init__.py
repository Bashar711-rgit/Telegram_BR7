# -*- coding: utf-8 -*-
"""════════════════════════════════════════════════════════════════════════
Telegram_BR7 — Lightweight Control Dashboard (v15.1.0)
════════════════════════════════════════════════════════════════════════

لوحة تحكم تشغيل ومراقبة خفيفة (Lightweight Dashboard):

  Lightweight · Event-Driven · Low Memory · Minimal Database Load

المبادئ التصميمية الصارمة (قرار هندسي — v15.1.0):
  * بلا اعتماديات جديدة إطلاقاً — aiohttp الموجود أصلاً لخادم الصحة يخدم
    اللوحة على نفس المنفذ $PORT تحت /admin (صفر processes إضافية).
  * فصل تام عن مسار استقبال الرسائل: معالجات اللوحة تعيش على حلقة خيط
    الصحة المعزول، وكل عملية ثقيلة تُجسَّر (bridge) إلى الحلقة الرئيسية
    عبر run_coroutine_threadsafe — لا تنشئ اللوحة Telethon client جديداً
    ولا تجمع اتصالات DB جديداً أبداً.
  * صفر استعلامات DB في مسار الطلب للقراءات المتكررة: كاش مشترك واحد
    (single-flight) يُحدَّث عند الطلب فقط (لا مهام خلفية لكل مستخدم).
  * لا يُقرأ أي ملف أصول من القرص عند كل طلب — تُحمَّل الملفات الثلاثة
    (html/js/css) إلى الذاكرة مرة واحدة عند التركيب.
  * الجلسات في الذاكرة حصراً (بلا كتابة DB لكل قراءة)، وسجل التدقيق
    (audit_logs الموجود أصلاً) يُكتب فقط للعمليات التعديلية.
  * لا تُكشف أسرار أبداً: Session Strings / API Hash / Bot Token / كلمة
    المرور لا تغادر العملية — فحص env يعرض set/unset حصراً.
  * مفاتيح الإيقاف (فلترة/تنبيهات) فحص ذاكرة صفرية التكلفة في المسار
    الحرج، ويُحفظ أثرها في app_settings (سطر واحد) ليبقى بعد إعادة التشغيل.

الوحدات الست: الحالة / الحسابات / الكلمات / المجموعات / التنبيهات / الإعدادات
المصادقة: bashar / bashar123r (قابلة للتجاوز عبر DASHBOARD_USERNAME /
          DASHBOARD_PASSWORD) — جلسات HttpOnly SameSite=Strict + CSRF
          (X-CSRF-Token) + Rate Limit + قفل تسجيل الدخول بعد المحاولات.

ملاحظة تاريخية: لوحة سابقة (FastAPI/uvicorn، 4595 سطراً) أُزيلت في v14.0
لثقلها — هذه النسخة مبنية بفلسفة معاكسة عمداً: نفس aiohttp، صفر deps،
صفر خيوط جديدة، صفر استعلامات في مسار الرسائل.
"""

from __future__ import annotations

DASHBOARD_VERSION: str = "15.3.0"

__all__ = ["DASHBOARD_VERSION"]

# ─── جسر سجلات اللوحة إلى loguru ──────────────────────────────────────────
# وحدات اللوحة تستخدم logging القياسي؛ هذا الجسر المحدود (namespace
# "dashboard" فقط) يوجّه سجلاتها إلى loguru لتبقى مرئية في bot.log
# وسجل Render — بلا أي أثر على بقية namespaces.
import logging as _stdlib_logging


class _DashLoguruBridge(_stdlib_logging.Handler):
    """يوصل سجلات logging القياسية لـ namespace اللوحة إلى loguru."""

    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        try:
            from loguru import logger as _loguru

            # loguru يتطلب أسماء المستويات بحروف كبيرة (WARNING/ERROR/...)
            try:
                level = record.levelname.upper()
                _loguru.level(level)
            except ValueError:
                level = "INFO"
            _loguru.opt(exception=record.exc_info).log(
                level, f"[dashboard.{record.name}] {record.getMessage()}"
            )
        except Exception:
            pass


_dash_root = _stdlib_logging.getLogger("dashboard")
if not any(isinstance(_h, _DashLoguruBridge) for _h in _dash_root.handlers):
    _dash_root.addHandler(_DashLoguruBridge())
    _dash_root.setLevel(_stdlib_logging.INFO)
    _dash_root.propagate = False

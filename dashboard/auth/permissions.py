# -*- coding: utf-8 -*-
"""dashboard.auth.permissions — الأدوار والصلاحيات (المرحلة 1، §6.2).

مصفوفة المواصفة:
  عرض البيانات            → الكل
  إدارة الحسابات          → owner/admin/operator
  إدارة المصادر           → owner/admin/operator
  الكلمات والقواعد        → owner/admin/operator
  إدارة الإعدادات         → owner/admin
  العمليات الحساسة        → owner/admin
  النسخ والاستعادة        → owner/admin
  إدارة المستخدمين        → owner
  إعدادات الأمان الرئيسية → owner

الصلاحيات تُطبَّق على الـBackend (الوسيط في server.py) — الواجهة تخفي
العناصر فقط ولا تُعد حماية (شرط صريح في المواصفة).
"""
from __future__ import annotations

from typing import List

ROLES = ("owner", "admin", "operator", "viewer")

_VIEW = frozenset({"view"})

_OPERATOR = _VIEW | frozenset({
    "accounts.manage",     # الحسابات: تشغيل/إيقاف/ربط/حذف
    "sources.manage",      # المصادر + الحظر/الأشخاص
    "filtering.manage",    # الكلمات + القواعد + Playground
    "alerts.manage",       # سجل/اختبار/إعادة إرسال التنبيهات
})

_ADMIN = _OPERATOR | frozenset({
    "settings.manage",     # الميزات + (المرحلة 2) مركز الإعدادات
    "ops.dangerous",       # عمليات الطابور/الذاكرة/إعادة التشغيل/إيقاف الفلترة
    "data.manage",         # النسخ الاحتياطي والاستعادة والتصدير
})

_OWNER = _ADMIN | frozenset({
    "users.manage",        # إدارة مستخدمي اللوحة
    "security.core",       # TOTP للغير + إعدادات الأمان الرئيسية
})

ROLE_PERMISSIONS = {
    "viewer": _VIEW,
    "operator": _OPERATOR,
    "admin": _ADMIN,
    "owner": _OWNER,
}


def normalize_role(role: str) -> str:
    r = str(role or "").strip().lower()
    return r if r in ROLES else "viewer"


def role_allows(role: str, perm: str) -> bool:
    if not perm:
        return True
    return perm in ROLE_PERMISSIONS.get(normalize_role(role), _VIEW)


def permissions_list(role: str) -> List[str]:
    return sorted(ROLE_PERMISSIONS.get(normalize_role(role), _VIEW))

# REMEDIATION LOG — جلسة v12.2.0

سجل كل تغيير: بلاغه، ملفاته، سببه، اختباراته، أثره التوافقي، ومتطلبات الترحيل (لا شيء في كل الحالات — صفر تغييرات Schema).

---

## 1) S-1 — توحيد حل عنوان العميل خلف البروكسيات

**الملفات:**
* `security.py` (+99 سطر): `parse_trusted_proxies`، `trusted_proxy_networks` (كاش 60s)، `_ip_in_trusted`، `resolve_client_ip` — وحدة مشتركة بلا تبعيات تطبيقية (يحترم قيد «dashboard لا يستورد webadmin»).
* `dashboard.py::_AuthGuard.client_ip` → تفويض للمحلل الموحد (مع سقوط أرضي آمن).
* `webadmin/auth.py::_LoginGuard._key` → تفويض للمحلل الموحد؛ حذف `_trusted_proxies`/`_ip_in_any_cidr`/كاشها المكرر (كود ميت بعد التوحيد)؛ إزالة استيراد ipaddress غير المستخدم.
* `render.yaml` (+6 أسطر): `TRUSTED_PROXIES=127.0.0.0/8,10.0.0.0/8,::1/128` موثق.
* `tests/test_auth_guard_retention.py`: تحديث توقع `test_client_ip_*` (السلوك المصحح) — موثق أعلاه.
* `tests/test_client_ip_security.py` (جديد، 17 اختباراً): وحدات الخوارزمية + إعدادات TRUSTED_PROXIES + انحدار هجومي الترويش/السَمّ + توازن webadmin.

**السبب**: بلاغ S-1 — أول قيمة XFF يتحكم بها المهاجم (ترويش قفل + سَمّ ضحية).
**الاختبارات**: 114 اختبار أذونات/دخول + المجموعة الكاملة 1150/1150.
**الأثر التوافقي**: سلوك الإنتاج يتغير بأمان — بعد النشر تصبح مفاتيح القفل (أقصى يمين XFF غير موثوق) بدل (أول قيمة). القيم المرسلة من بروكسي Render (يضبط XFF بنفسه) غير متأثرة عملياً؛ المهاجمون يفقدون قدرة الترويش.
**ترحيل**: لا شيء (متغير env جديد بقيمة افتراضية معقولة داخل الكود — إضافة render.yaml تصريحية).

**حادثة أثناء التنفيذ (موثقة بالصدق)**: أول تشغيل للاختبارات الجديدة فشل واسعاً — السبب: `security.trusted_proxy_networks` أسند `_proxy_cache` بدون `global` (UnboundLocalError التمهه الفشل-الآمن وعاد للمباشر بصمت) + `import time` غائب. الإصلاح من سطرين، والاختبارات صارت تثبت المسار السليم فعلاً — درس: الاختبار الذي «ينجح للسبب الخطأ» أخطر من فشله.

## 2) F-14 — مسار احتياطي لإزالة التشكيل بلا pyarabic

**الملفات:**
* `filter_engine.py`: `_FALLBACK_TASHKEEL_RE` (064B-065F + 0670 + 0640) + فرع `_normalize_arabic` عند `PYARABIC_AVAILABLE=False`.
* `tests/test_filter_engine.py`: `test_tashkeel_stripped_without_pyarabic` — يفرض غياب pyarabic ويثبت سلوك araby المكافئ + بقاء التطبيع الأساسي.

**السبب**: بلاغ F-14 — غياب التبعية كان يُسقط إزالة التشكيل بصمت (FN).
**الاختبارات**: 83/83 ملفات الفلتر + المجموعة الكاملة.
**الأثر التوافقي**: لا تغيير عندما تتوفر pyarabic (الإنتاج) — تغطية حماية فقط.
**ترحيل**: لا شيء.

## 3) S-3 — تصحيح تعليقات الأنواع (F821)

**الملفات:** `monitors.py` (استيراد deque صريح بدل `__import__` hack)، `unified_alerts/maintenance.py` (Dict في typing imports).
**السبب**: حللية نظيفة + إمكانية `get_type_hints` مستقبلاً. **الاختبارات**: المجموعة الكاملة. **ترحيل**: لا شيء.

## 4) نظافة الاختبارات

**الملفات:** `tests/test_features.py` — إزالة `@pytest.mark.asyncio` من مستوى الفصل (كانت تلصق اختباراً متزامناً) ووضعها صراحة على الاختبارات غير المتزامنة الخمسة.
**السبب**: تحذير pytest المتكرر في كل تشغيل. **الترحيل**: لا شيء.

## 5) bump الإصدار

`config.py`: `BOT_VERSION "12.1.0" → "12.2.0"` — الشارة في اللوحة تُحقن ديناميكياً (منذ v11.7.1) فستصحح تلقائياً بعد النشر.

## 6) الوثائق (هذه الجلسة)

`docs/audit/BASELINE.md`، `FINDINGS_REGISTER.md`، `TEST_REPORT.md`، هذا الملف، `DATABASE_AUDIT.md`، `SECURITY_AUDIT.md`، `docs/architecture/ARCHITECTURE.md`، `FILE_INVENTORY.md`، `docs/development/DEVELOPMENT_PLAN.md`، `docs/deployment/DEPLOYMENT_GUIDE.md`، `RELEASE_CHECKLIST.md`، `docs/operations/RECOVERY_AND_BACKUP.md`.

## ما لم يتغير عمداً

* جدول `br7` الموازي على Supabase (D-1 — يحتاج قرار مالك).
* استيرادات/متغيرات ميتة موثقة (W-1/W-2) — تقليل تشويش الفروقات.
* سباق dead-letter المزدوج النظري (V12-D) — نافذة ميليمترية بلا أثر مستخدم؛ الإصلاح يجر معاملة معقدة.

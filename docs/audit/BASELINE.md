# BASELINE — جلسة التدقيق الهندسي v12.2.0

التاريخ: 2025-10-30 | الفرع: `main` | HEAD عند البدء: `c518e0a` (v12.1.0)
المستودع: `Bashar711-rgit/Telegram_BR7` — شجرة العمل نظيفة عند البدء (صفر تعديلات مستخدم غير ملتزمة).

## 1. حالة المستودع عند البدء

| البند | القيمة |
|---|---|
| آخر commit | `c518e0a` — v12.1.0: solid-sync pipeline (claim/ack/recover) + unified filter policy + reference card format |
| تزامن origin | `main...origin/main` بدون ahead/behind (متزامن بعد fetch) |
| تعديلات المستخدم غير الملتزمة | لا شيء (`git status` نظيف) |
| سلسلة الإصدارات | v11.3 → v11.5.x (فلترة 6..65 + قالب 💬) → v11.6 (قالب «المرسل:» + أزرار) → v11.7.x (إصلاحات تدقيق + PG) → v11.8.x (intent) → v12.0.x (unified_alerts) → v12.1.0 (claim/ack/recover) |

## 2. بيئة التشغيل المحلية

| البند | القيمة |
|---|---|
| Python | 3.12.16 (uv-managed، venv معزول `/home/z/.venv` خارج شجرة المصدر) |
| pytest | متوفر (`pytest.ini` بالمشروع) |
| العتماديات المثبتة | مطابقة `requirements.txt` بعد استكمال 4 نواقص بيئية (انظر §4) |

## 3. أوامر خط الأساس المنفذة

```bash
python -m py_compile *.py webadmin/*.py unified_alerts/*.py tests/*.py scripts/*.py   # PASS
python -m pytest -q --tb=short -p no:cacheprovider                                    # المجموعة الكاملة
python -m ruff check --select=E9,F63,F7,F82,F401,F841 *.py webadmin/*.py unified_alerts/*.py
```

## 4. حوادث البيئة (ليست عيوب كود — موثقة لمنع تكرارها)

1. **venv ناقص 4 اعتماديات**: `langdetect`, `pyarabic`, `uvloop`, `itsdangerous` كانت غائبة عن البيئة المحلية رغم وجودها في `requirements.txt` (بيئة وضعت حديثاً). **النتيجة الظاهرية**: 12 اختباراً فاشلاً (test_webadmin/test_rbac/test_filter_engine).
   * فشل webadmin/رباك: `webadmin dashboard not loaded: No module named 'itsdangerous'` — mount_admin يفشل صامتاً (تحذير لوج فقط).
   * فشل الفلتر: `PYARABIC_AVAILABLE=False` → التشكيل لا يُزال → تطابق عربي مكسور.
   * **العبرة الهندسية**: النمطان مكشوفان لغياب تبعية — أُغلق نمط الفلتر بجذر (F-14)؛ نمط webadmin بقي تحذيراً فقط (انظر FINDINGS_REGISTER S-4).
2. **تشغيل pytest في الخلفية**: العملية تُقتل مع نهاية جلسة الأمر في هذه البيئة — المجموعة تُشغَّل في المقدمة.

## 5. نتائج خط الأساس

| التشغيل | النتيجة |
|---|---|
| v12.1.0 كما هو (بعد استكمال بيئة) | **1133/1133 PASS** (1121 pass + 12 فشل بيئي استُكملت بيئته) — EXIT=0 |
| py_compile لكل الملفات | PASS (صفر أخطاء تركيب) |
| ruff (E9,F63,F7,F82) | 5 F821 (تعليقات أنواع فقط — أُصلحت S-3) |
| ruff (F401,F841) | 19 F401 + 5 F841 — بقايا نظافة بلا أثر تشغيلي (موثقة) |

## 6. قاعدة البيانات الإنتاجية (فحص قراءة-فقط على Supabase الحي)

| البند | النتيجة |
|---|---|
| المستخدم | `br7_app` (Session Pooler، sslmode=require) |
| المخطط الفعلي للتطبيق | **`public`** — alerts=336، messages=34012، alert_dedup=77، dead_letters=0، processing_queue=6 |
| ترحيل 0003 (claim/ack/recover) | **مُطبق فعلاً على public.processing_queue** — أعمدة status/claimed_at/attempts موجودة ✓ |
| مخطط `br7` (44 جدولاً) | **موجود بلا صلاحيات لـ br7_app** (InsufficientPrivilege على كل COUNT) — نسخة موازية غير مستخدمة من ترحيل v11.8.2 (انظر D-1 في FINDINGS_REGISTER) |
| بحث المسار | `search_path` الافتراضي `"$user", public` — التطبيق يعمل على public |

## 7. القيود المعروفة عند البدء

* لا دفع إلى origin دون تصريح صريح (عقد §25) — الالتزامات تبقى محلية حتى الموافقة.
* لا اختبار حي على تيليجرام إنتاجي (§2 يمنع رسائل حقيقية) — mocks + قناة اختبار فقط عند توفر توكن، ولم يُطلب هذا الجلسة.
* venv الإنتاج (Render) يبني من requirements.txt — الفحوص هنا ببيئة معاد بناؤها تحاكيها.

## 8. المخاطر المعروفة الموروثة (من worklog، لم تُمس)

* تحذير EPHEMERAL STORAGE إذا سقط DATABASE_URL (يوثّح نفسه بلوج CRITICAL — سلوك مقصود).
* توكنات مكشوفة في محادثات سابقة تحتاج تدويراً من المالك (خارج نطاق التنفيذ الآلي).

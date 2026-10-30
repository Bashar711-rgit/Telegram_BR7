# FINDINGS REGISTER — جلسة v12.2.0

كل بلاغ بمعرّف فريد، شدة، ثقة، دليل، جذر، إصلاح، واختبار. الحالات: FIXED / OPEN / DOCUMENTED.

---

## S-1 — ترويش قفل brute-force وسَمّ الضحية عبر X-Forwarded-For (dashboard)

* **الشدة**: عالية (أمن) | **الثقة**: مؤكدة (أُعيد إنتاجها بجزاء وحدوي)
* **الموضع**: `dashboard.py::_AuthGuard.client_ip` (كان يقبل أول قيمة XFF حرفياً)
* **الدليل**: `tests/test_client_ip_security.py::test_xff_rotation_attack_cannot_evade_lockout` و`test_victim_poisoning_blocked` — يفشلان على كود ما قبل v12.2.0 (مفاتيح تتبدل مع كل تزوير / تُنسب فشلات المهاجم للضحية).
* **الأثر**: خلف بروكسي لا يستبدل الترويسة: مهاجم يبدّل XFF كل طلب لا يبلغ عتبة القفل أبداً؛ ويمكن قفل IP مدير حقيقي بفشلات مزوّرة (DoS إداري).
* **الجذر**: تكرار النمط الضعيف الذي أصلح سابقاً في webadmin (v9.12 M-12) فقط — نسختان مختلفتان لنفس الحاجة.
* **الإصلاح (FIXED)**: مُحلّل موحد `security.resolve_client_ip` (خوارزمية البروكسي الموثوق المعيارية: نظير غير موثوق → تجاهل XFF كلياً؛ وإلا مشي يمين-يسار متخطياً القفزات الموثوقة؛ سقوط آمن للمباشر)؛ استهلكه `dashboard._AuthGuard` و`webadmin._LoginGuard` معاً؛ حذف الكود المكرر من webadmin؛ `TRUSTED_PROXIES` موثق في render.yaml (الافتراضي: loopback + 10/8).
* **الاختبارات**: 17 اختباراً جديد (`tests/test_client_ip_security.py`) + تحديث `test_client_ip_rightmost_untrusted_xff` (كان يرمّز السلوك الضعيف) + 114 اختبار أذونات/دخول PASS.
* **قيود متبقية**: من يستطيع الاتصال من داخل نطاق موثوق فعلاً (البروكسي نفسه) يظل قادراً نظرياً على التأثير في المفاتيح — ضمن نموذج التهديد المعياري.

## F-14 — إسقاط صامت لإزالة التشكيل عند غياب pyarabic (فلترة)

* **الشدة**: متوسطة (موثوقية مطابقة) | **الثقة**: مؤكدة (12 فشل اختبار في بيئة نظيفة)
* **الموضع**: `filter_engine.py::_normalize_arabic` — كان يعتمد `araby` حصراً.
* **الدليل**: `tests/test_filter_engine.py::TestArabicNormalization::test_diacritics_do_not_break_matching` فشل بلا pyarabic («اِحْتَـاجُ حَـلّ» لا تطابق «احتاج حل»).
* **الأثر**: أي بيئة بلا pyarabic → رسائل مشكولة لا تُطابق الكلمات المفتاحية → FN (تنبيه صالح يُفقد) بلا أي لوج.
* **الجذر**: تبعية اختيارية تُدار بـ flag لكن المسار البديل كان «لا تفعل شيئاً» بدل مكافئ دلالي.
* **الإصلاح (FIXED)**: مسار احتياطي regex (`_FALLBACK_TASHKEEL_RE` — 064B-065F + 0670 + 0640) بنفس دلالة strip_tashkeel/strip_tatweel.
* **الاختبارات**: `test_tashkeel_stripped_without_pyarabic` (يفرض PYARABIC_AVAILABLE=False ويطابق سلوك araby) + الملف الكامل PASS.

## D-1 — انحراف مخطط: مخطط `br7` الموازي غير المستخدم على Supabase

* **الشدة**: متوسطة (تشغيلي/توثيقي — لا خلل سلوك) | **الثقة**: مؤكدة (فحص قراءة-فقط على الحي)
* **الدليل**: `pg_tables` يُظهر 44 جدولاً في `br7` بلا أي صلاحيات لـ `br7_app` (كل COUNT يفشل InsufficientPrivilege)؛ جداول التطبيق الحية في `public` (alerts=336، messages=34012، alert_dedup=77، dead_letters=0، processing_queue=6)؛ ترحيل 0003 (status/claimed_at/attempts) **مُطبق فعلاً على public** ✓.
* **الجذر**: ترحيل v11.8.2 («br7 unified schema v1») طُبق على مخطط `br7` بينما التطبيق ينشئ جداوله بنفسه على `public` (search_path الافتراضي).
* **الخطر**: التشغيل المستقبلي للترحيلات على `br7` يظل بلا أثر؛ ومشغّل يفحص br7 يظن أنه الحالة الفعلية.
* **الحالة (OPEN — خارج نطاق التفويض)**: يتطلب قراراً من المالك: إما (أ) توثيق `public` كمخطط رسمي وإهمال br7، أو (ب) منح USAGE/GRANTS وترحيل التطبيق إلى br7. لا تعديل مخطط إنتاجي دون تصريح (§2).

## S-3 — تعليقات أنواع بمسميات غير مستوردة (F821)

* **الشدة**: منخفضة (نظافة — لا انهيار وقت تشغيل) | **الثقة**: مؤكدة (ruff)
* **الموضع**: `monitors.py:692-693,710` (`deque` عبر `__import__` hack) + `unified_alerts/maintenance.py:58,63` (`Dict` غير مستورد).
* **الإصلاح (FIXED)**: استيراد صريح `from collections import deque` + إضافة `Dict` لاستيرادات typing — صفر تغيير سلوك.
* **الاختبارات**: المجموعة الكاملة PASS بعد الإصلاح.

## W-1..W-3 — بقايا نظافة موثقة (لا تُصلح عمداً في هذه الجلسة)

* **W-1**: 19 F401 (استيرادات غير مستخدمة) — بعضها مقصود (توافر كسول/إعادة تصدير)؛ إزالتها الجماعية تغمّر diff بلا قيمة صحي. **DOCUMENTED**.
* **W-2**: 3 F841 (متغيرات محلية ميتة: `contact_target.py:226 sid`، `database.py:1080 ok`، `main.py:983 health`) — كلها نواتج مسارات أُعيدت هيكلتها سابقاً؛ لا أثر تشغيلي. **DOCUMENTED**.
* **W-3**: تحذير teardown `aiosqlite` في الاختبارات (`Event loop is closed` بعد اكتمال المجموعة) — تلوث إغلاق بعد نهاية الحلقة، لا يؤثر على النتائج (EXIT=0). إصلاحه يتعمق في دورة حياة اتصالات الاختبار — مؤجل مع سبب. **DEFERRED_WITH_REASON**.

## S-4 — mount_admin يفشل صامتاً عند نقص اعتمادية (لوحة إدارية كاملة لا تُحمَّل)

* **الشدة**: متوسطة (ملاحظة تشغيلية) | **الثقة**: مؤكدة (أُعيد إنتاجها في البيئة الناقصة)
* **الموضع**: `dashboard.py:4569-4572` — `except Exception: logger.warning(...)`.
* **الدليل**: بلا itsdangerous: `/admin` وكل REST الإدارية 404 بصمت نسبي (تحذير لوج واحد عند الإقلاع).
* **التقييم**: الفشل-الآمن مقصود (لوحة اختيارية لا تُسقط البوت) — لكنه يخفي انقطاعاً كاملاً للميزة. الإنتاج الحالي يملك الاعتمادية (requirements.txt) فلا تأثير حي.
* **الحالة (OPEN — توصية)**: إضافة فحص جاهزية يظهر في `/health/full` (webadmin_mounted: true/false) — ترشيح لجلسة قادمة؛ لا يُنفذ الآن لتقييد نطاق التغيير الجلسة إلى ما ثبتت حاجته.

## V12-D — ملاحظات تصميم solid-sync (claim/ack/recover) — مراجعة بلا عيوب مفتوحة

* تدقيق القراءة الكامل لـ `pop_from_queue`/`ack_queue_event`/`recover_stale_inflight`/`run_startup_maintenance` + حلقة producer/worker في main.py:
  * SQLite: claim تحت قفل asyncio واحد — آمن أحادي العملية؛ PG: `FOR UPDATE SKIP LOCKED` — آمن متعدد المثيلات. ✓
  * المسارات الاستثنائية (إيقاف/استثناء/لا مراقب) تعيد الإدراج priority=9 بينما يبقى الصف in_flight → ازدواج منطقي يقطعه dedup L1/L2 عند إرسال فعلي سابق — at-least-once موثق مقابل فقدان كلي. ✓ (سياسة معلنة، ليست عيباً)
  * سباق نظري واحد حُدد: حلقتا استرداد متزامنتان (إقلاع + دورية) قد تدخلان dead-letter مزدوجاً لنفس الصف (INSERT قبل DELETE المحروس بـ status='in_flight') — نافذة ميليمترية، أثرها صف مكرر في dead_letters بلا أثر مستخدم. **DOCUMENTED** (إصلاح يتطلب معاملة UPDATE-THEN-INSERT المحصورة — يُقيَّم لاحقاً).

---

# إضافات جلسة v12.3.0

## V12-D — سباق ازدواجية dead-letter (حُدد سابقاً DOCUMENTED → الآن FIXED)

* **الشدة**: منخفضة (صف مكرر في dead_letters بلا أثر مستخدم) | **الثقة**: مؤكدة (أُعيد إنتاجها باختبار متزامن)
* **الموضع**: `database.py::recover_stale_inflight` — INSERT→DELETE كعمليتين مستقلتين.
* **الدليل**: `tests/test_v12_3_race_and_health.py::test_concurrent_recovery_no_duplicate_dead_letter` — على كود ما قبل v12.3 ينتج حرفين ميتين لنفس الحدث (كود الاسترداد يحصر الصفوف قبل أن يكتب أيٌّ منهما).
* **الإصلاح (FIXED)**: قفل CAS ذرّي على attempts (`UPDATE ... WHERE id=? AND status='in_flight' AND attempts=?` + `attempts=attempts+1`) — الفائز وحده يكتب INSERT+DELETE (معاملة واحدة PG / قفل+commit فوري SQLite). فرع الاسترداد صار CAS أيضاً فلا يطمس حجز مستهلك حديث. أثر جانبي مقصود: attempts يحصي كل بدء معالجة → حدّ QUEUE_INFLIGHT_MAX_ATTEMPTS يُنهي أحداث السمّ (اختبار test_poison_event_terminates_via_recover_cycles).
* **الاختبارات**: 6 اختبارات V12-D (سباق متزامن حقيقي، CAS خاسر، عقد retry_count، إنهاء حدث السمّ، عدّ صادق).
* **قيود متبقية**: فروع PG في _dead_letter_row/_execute_affected مغطاة نصياً (بنية مطابقة لمسارات PG قائمة) — الإثبات التشغيلي يتطلب مثيل PG معزول (P2-6).

## S-4 — mount_admin يفشل صامتاً (كان OPEN توصية → الآن FIXED)

* **الإصلاح (FIXED)**: `app.state.webadmin_mounted` (False قبل المحاولة / True بعد النجاح) + `app.state.webadmin_mount_error` (نص مقطوع 200 حرفاً) — منضبطان في كتلة التركيب نفسها، ومكشوفان في `/health/full`.
* **الاختبارات**: 3 اختبارات (المفاتيح بنوعيهما في الحمولة؛ العلم عند الاستيراد؛ الصدق السلوكي: العلم True ⇒ GET /admin لا يعيد 404).

## F-15 — خاصية app.routes لا تعكس include_router في FastAPI 0.162.0 (DOCUMENTED)

* **الشدة**: منخفضة (فخ تأمل — لا خلل سلوك) | **الثقة**: مؤكدة (أُعيد إنتاجها 3 مرات بتتبع include_router)
* **الدليل**: `include_router` استدعي على التطبيق الصحيح براوتر يحمل 35 مساراً (before=77 → after=78 في app.router.routes) بينما `app.routes` يظهر 78 مساراً بلا أي مسار /admin — وGET /admin يستجيب 200 فعلاً وGET /keywords يعيد 401 (الحماية تعمل). التوجيه سليم؛ الاستبطان الكاذب.
* **الأثر**: أي كود/اختبار يتحقق هيكلياً عبر app.routes قد يحكم كذباً أن الميزة غير مركّبة (كاد يُنتج إنكاراً كاذباً في اختبار v12.3 نفسه).
* **الحالة (DOCUMENTED)**: القاعدة المتبناة: التحقق سلوكي (طلب HTTP حقيقي) لا استبطاني. مرجع: docstring test_mounted_flag_matches_router_reality.

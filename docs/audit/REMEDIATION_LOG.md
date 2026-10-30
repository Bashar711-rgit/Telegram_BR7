# REMEDIATION LOG — جلسة v12.2.0

سجل كل تغيير: بلاغه، ملفاته، سببه، اختباراته، أثره التوافقي، ومتطلبات الترحيل (لا شيء في كل الحالات — صفر تغييرات Schema).

---

# REMEDIATION LOG — جلسة v12.3.0 (نشر + إغلاق P2)

## 1) نشر v12.2.0 إلى الإنتاج (P1-2)

* دفع `906dd7a` → origin/main (بتوكن المالك المخزن في /home/z/.secrets — بلا عرض) → Render autoDeploy `dep-dbi177p7` → **live**.
* تحقق حي: `/health` → status=ok، database=ok (PostgreSQL متصل)، db_healthy=true.
* خط الأساس قبل الدفع: **1151/1151 PASS** (EXIT=0) + فحص أسرار على الفرق (سالب) + py_compile.

## 2) V12-D — حصر سباق ازدواجية dead-letter (كان موثقاً «لم يتغير عمداً»)

**الملفات:**
* `database.py::_execute_affected` (جديد): عدد الصفوف المتأثرة على المحركين (sqlite rowcount / asyncpg tag "UPDATE n") — فشل → 0 (الاتجاه الآمن).
* `database.py::_dead_letter_row` (جديد): claim ذرّي CAS على attempts (`WHERE id=? AND status='in_flight' AND attempts=?` ثم `attempts=attempts+1`) → الفائز وحده يكتب INSERT+DELETE (معاملة واحدة على PG؛ قفل _queue_lock + commit فوري على SQLite).
* `database.py::recover_stale_inflight`: الفرع الميت يفوض لـ_dead_letter_row؛ فرع الاسترداد صار CAS أيضاً (لا يطمس حجز مستهلك حديث — المستهلك يزيد attempts أيضاً). الزيادة في الاسترداد تعني: attempts يحصي كل بدء معالجة → حدّ QUEUE_INFLIGHT_MAX_ATTEMPTS يُنهي أحداث السمّ بدل الارتداد الأبدي pending↔in_flight.
* `tests/test_v12_3_race_and_health.py` (جديد، 6 اختبارات V12-D): سباق متزامن حقيقي (asyncio.gather) → حرف ميت واحد بالضبط؛ استرداد متزامن → مرة واحدة؛ CAS يخسر بقيمة قديمة بلا كتابة؛ عقد retry_count=attempts محفوظ؛ حدث السمّ ينتهي؛ _execute_affected صادق.

**السبب**: مسارا استرداد متزامنان يقرآن نفس الصفوف العالقة → حرف ميت مكرر لنفس الحدث (السجل السابق قيّده «نافذة ميليمترية بلا أثر» — صُحح الآن لأن الإصلاح أمكن بلا معاملة معقدة: CAS وحيد + بنية معاملة موجودة).
**الاختبارات**: 1160/1160 PASS (كان 1151 + 9 جدد). **ترحيل**: لا شيء (دلالة retry_count القائمة محفوظة عمداً).

## 3) S-4 — شفافية تركيب webadmin في /health/full

**الملفات:**
* `dashboard.py` (كتلة التركيب): `app.state.webadmin_mounted` (False قبل المحاولة، True بعد النجاح) + `app.state.webadmin_mount_error` (نص الخطأ عند الفشل) — التحذير في اللوج بقي.
* `dashboard.py::health_full`: حقلا `webadmin_mounted` (bool) و`webadmin_mount_error` (مقطوع 200 حرفاً) — فشل التركيب لم يعد صامتاً في التشخيص.
* `tests/test_v12_3_race_and_health.py` (3 اختبارات S-4): المفتاحان موجودان بنوعيهما؛ العلم مضبوط عند الاستيراد؛ الصدق السلوكي (العلم True ⇒ GET /admin لا يعيد 404).

## 4) اكتشاف تدقيقي جديد F-15 (DOCUMENTED — لا خلل سلوك)

**FastAPI 0.162.0**: خاصية `app.routes` لم تعد تعكس مسارات `include_router` (أظهرت 78 مساراً بلا أي مسار /admin بينما GET /admin يستجيب 200 فعلاً — أُعيد إنتاجها ثلاث مرات بتتبع include_router: before=77/after=78 بينما الراوتر يحمل 35). كل تحقق هيكلي قائم على app.routes في الكود/الاختبارات يجب أن يكون سلوكياً (طلب HTTP حقيقي). موثق في docstring الاختبار.

## 5) bump الإصدار

`config.py`: BOT_VERSION "12.2.0" → "12.3.0".

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
* ~~سباق dead-letter المزدوج النظري (V12-D)~~ — **أُغلق في v12.3.0** بقفل CAS ذرّي (انظر قسم v12.3.0 أعلاه).

---

# REMEDIATION LOG — جلسة v13.0 (إصلاح جذري لتكرار التنبيهات + بطاقة موحدة)

**مصدر المهمة**: تعليمات المالك الحرجة — «تنبيه واحد لكل رسالة منطقية جديدة عبر كل الحسابات (7+)، وبطاقة موحدة: B_Bot ← المرسل ← نص الرسالة ← زرا مراسلة/عرض الرسالة». تنفيذ فعلي مع اختبارات، لا خطة نظرية.

**اكتشاف تدقيقي أولي**: شجرة العمل احتوت تعديلات v12.4.0 غير مكتملة من جلسة متوازية مقطوعة — أبرزها استيراد `BOT_NAME_LINE` من contact_target **دون تعريفه** (ImportError يمنع إقلاع monitors.py كلياً). أُكملت التعديلات (إيصال فوري، بوابة إيصال، توسيع نافذة المُحقّق، توقيع الصدى) ودمجت مع إصلاحات v13 أدناه.

## RC-1 — سباق «فك-ثم-احجز» في المسار المسترد (CRITICAL — سباق متزامن فعلي)

**السبب**: `_send_alert` كان يفك حجز dedup غير مشروط لكل حدث `_recovered` قبل المطالبة. عاملان مستردّان لنفس المحتوى: A يحجز → B يفك حجز A الطازج → B يحجز → **تنبيهان مكرران** (TOCTOU مثبت بقراءة الكود).

**الإصلاح**:
* `database.py::steal_stale_alert_fingerprint` (جديد): حذف شرطي ذري واحد `DELETE ... WHERE fingerprint=? AND last_seen < cutoff` — rowcount هو حكم CAS الوحيد على المحركين (لا check-then-act).
* `dedup.py::AlertDeduplicator.steal_if_stale` (جديد): فك الذاكرة + الاستيلاء المشروط + عدادان (stolen/steal_denied).
* `monitors.py::_send_alert`: إلغاء الفك غير المشروط. الترتيب الجديد: إيصال (v12.4.0) → مطالبة → عند الحجب فقط وبلا إيصال: استيلاء على حجز عتيق (≥ `DEDUP_RECOVERY_STEAL_MIN_AGE_SECONDS`=120، أقصر بمراحل من عمر الاسترداد 600s) → إعادة مطالبة؛ حجز طازج لعامل حي = انسحاب بلا تكرار.
* `config.py`: إعداد حي جديد `DEDUP_RECOVERY_STEAL_MIN_AGE_SECONDS` (افتراضي 120).

## RC-2 — إرسال مزدوج عند فشل التسجيل بعد نجاح الإرسال (CRITICAL — بلاغ مستخدم)

**السبب**: `do_send()` (إرسال فيزيائي ناجح) و`_record_sent_alert()` (كتابة DB) في try واحد — أي استثناء DB بعد نجاح الإرسال كان يدخل فرع الـexcept فيرسل نسخة احتياطية فورية = بطاقة مكررة، وقد تليها ثالثة عبر DLQ.

**الإصلاح** (monitors.py): فصل كامل — إرسال في try مستقل (الفشل فقط يفعّل fallback/DLQ)، والتسجيل في try منفصل بعده (الأساسي والمسار الاحتياطي): فشل التسجيل يُسجل بصدق + عداد unified `record_after_send_failed` **ولا يعيد الإرسال أبداً**.

## RC-3 — إعادة إرسال DLQ بلا فحص إيصال

`_retry_send_alert`: حارس `alert_exists(msg_hash)` قبل أي محاولة — تنبيه وصل فعلاً لا يُعاد (فشل الفحص نفسه لا يمنع المحاولة).

## RC-4 — دمج خاطئ في بصمة المحتوى

`dedup.content_fingerprint(sender_id, text, chat_id=None, media_kind=None)`: المرسل بلا معرف يُدار بمجال `v1:anon:{chat_id}` (لا دمج مرسلين مجهولين مختلفين؛ تكرار الحسابات لنفس حدث المجموعة يبقى محجوباً)، ونوع الوسائط يدخل المجال (نص متطابق بمرفق مختلف = هوية مختلفة — شرط الاختبار 9).

## البطاقة الموحدة v13 (مواصفة §9/§10/§14)

* `contact_target.py`: `BOT_NAME_LINE = "B_Bot"` مضمّن داخل `SENDER_LABEL` — كل الإزاحات المشتقة (كيانات alert_bot/alert_ui/entity_pack/الاختبارات) صحيحة تلقائياً.
* `alert_bot.build_alert_buttons`: الترتيب الحرفي `[ مراسلة ] [ عرض الرسالة ]`.
* `monitors.is_alert_card_echo`: توقيع ثلاثي (B_Bot\nالمرسل: | المرسل: | 👤:) — حاجز الصدى يتعرف على الجديد والقديم.
* مواصفة الاسم ثابتة كثابت مسمى واحد (مصدر وحيد) — تغييره من نقطة واحدة ينتشر لكل المستهلكين.

## الاختبارات

* `tests/test_v13_dedup_and_card.py` (جديد): الحالات العشر الإلزامية T1-T10 (7 حسابات، تزامن gather على مستوى dedup وعلى مستوى DB، رسالتان/مرسلان، إعادة تشغيل، استيلاء CAS طازج/عتيق/ذري، فك بعد فشل، إيصال فوري، هوية المرفقات، النافذة) + عقد البطاقة §14 (15 فحصاً).
* تحديث الاختبارات المثبتة على القالب القديم: golden regression + template v11.6 + contact_target + alert_ui + alert_bot v10.8 + message_context + nav_resolver + unified_v12.1 — إلى العقد v13 (باسم البوت وترتيب الأزرار الجديد).
* **المجموعة الكاملة: 1225 PASS — EXIT=0، صفر فشل** (كانت القاعدة 1160).

**ترحيل**: لا شيء — صفر تغييرات Schema (alert_dedup/alerts بنيتها القائمة تكفي؛ الإيصال الفوري يستخدم INSERT OR IGNORE على message_hash UNIQUE القائم).

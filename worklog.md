
---
Task ID: v11.5-length-template-F
Agent: Z.ai Code (main orchestrator)
Task: بروتوكول التنفيذ الهندسي — فلترة الطول 6..65 + توحيد القالب «💬:» + الرد التعويضي F (مواصفة 20 قسماً)

Work Log:
- البيئة أُعاد ضبطها: /home/z/telegram_br7 اختفى — استُعيد المستودع من GitHub (Bashar711-rgit/Telegram_BR7) عند /home/z/Telegram_BR7؛ main على 0db8941 (v11.3) — **التزامات v11.4 (سلّم T كامل + 887 اختباراً) ضاعت مع الصندوق القديم ولم تُدفع**.
- خط الأساس: 868 ناجحاً على v11.3.
- السبب الجذري لفلترة الطول (بالدليل): config.py:724-725 كانت MIN=10/MAX=5000 — كل البوابات (monitors 2010/2235/2243 + filter_engine:1538 + InputSanitizer:1016) تقرأ هاتين القيمتين؛ مسار الاستعادة/الـDLQ يعيد الفحص عبر _process_event_pipeline→_validate_event (monitors:2198).
- القالب: v11.3 بلا سطر «💬:» — المستخدم يتطلبه في §7 → أُعيد للطريقين (Bot API HTML + MTProto entity pack مع إعادة حساب إزاحات UTF-16).
- reply_fix.py جديد (مواصفة §11): F كامل بالحدود (ساعة/يوم/jitter/بث/أخطاء كتابة/تحقق كيان إلزامي/عزل hash للحساب المالك).
- [ALERT] موحّد + عدادات السلالم T/N/B/V + reply_fix_* (§16).
- golden: 6 ملفات اختبار حُدّثت للقالب الجديد + عينتا keywords.json الأطول من 65 استُبدلتا محلياً في test_filter_accuracy (keywords.json نفسه لم يُمس).
- قرار موثق: أعمدة alerts الأربعة (name_method/view_method/owner_account/tier) لم تُنفَّذ — بديل مناسب موجود (سطر [ALERT] + metrics + أعمدة v10.7/v11.2) وتعديل مسار الكتابة الدفعي يخالف «أقل تعديل».
- النتيجة: **911 ناجحاً** (868 + 43 جديد) — commit محلي 04ac29f، BOT_VERSION=11.5.0.
- نسخ احتياطية قبل التعديل: /home/z/backups_br7_v11_5/ (خارج المستودع).

Stage Summary:
- المتبقي على المستخدم: (1) الدفع إلى GitHub يتطلب توكلاً صالحاً (المحاولات السابقة 401) — أو دفع 04ac29f بنفسه؛ (2) ALERT_BOT_TOKEN في Render + البوت أدمن في TARGET_GROUP_ID؛ (3) التحقق الحي E2E على تيليجرام حقيقي لم يُنفَّذ (mocks فقط — بروتوكول §9 يمنع رسائل الإنتاج).
- القيود: خريطة حد الساعة لF بذاكرة العملية (تصفّر بعد restart — موثق داخل الوحدة).

---
Task ID: 3 (توكنات المستخدم — دفع النشر + عزل الاختبارات)
Agent: Z.ai Code (main orchestrator)
Task: استلام توكن GitHub وتوكن بوت التنبيهات، دفع commits محلية، إعادة التحقق من خط الأساس، ومطابقة v11.5 مع المواصفة

Work Log:
- استلام التوكنين من المستخدم؛ التحقق من توكن البوت عبر getMe → صالح: @BZ711BR_bot (id 7886937106).
- تخزين آمن: accounts.env (gitignored) يحمل ALERT_BOT_TOKEN + ALERT_BOT_ENABLED=true للبيئة المحلية؛ توكن GitHub في /home/z/.secrets/ خارج المستودع (لا أسرار في worklog أو الكود).
- دفع الـcommits المحلية المعلّقة: 0db8941..be4fdf2 (v11.5 Length gate 6-65 + قالب «💬:» + reply-fix F) → تفعيل نشر Render تلقائياً.
- إعادة بناء بيئة الاختبار في هذه الآلة (uv venv .venv + requirements + pytest pytest-asyncio httpx beautifulsoup4).
- اكتشاف وإصلاح ثغرة عزل حقيقية: مع وجود accounts.env على المضيف، توكن حقيقي تسرب إلى بيئة الاختبار وفشل test_alert_bot_config_contract (كان يفترض لا-توكن). الإصلاح (v11.5.1): conftest.py يثبّت ALERT_BOT_TOKEN="" و ALERT_BOT_ENABLED="true" قبل استيراد config (load_dotenv بلا override لا يجلبهما)، وعقد الاختبار صار falsy-check (أقوى: يرفض أي توكن فعّال). أيضاً تحديث docstring المتقادمة في alert_bot.py لقالب §7 (سطر «💬:» مستعاد).
- مطابقة المواصفة (قراءة الكود فعلياً):
  * حد النص 6..65 على النص الخام: config MIN=6/MAX=65؛ monitors._validate_event (قبل التنبيه)؛ validated_text في _store_message؛ مسار إعادة المحاولة DLQ → process_event_from_queue → _process_event_pipeline → _validate_event يعيد الفحص فعلاً؛ اختبار test_queue_restore_reruns_gate يغطيه.
  * قالب §7: 👤 + (فارغ) + 💬: + blockquote للنص الأصلي + [ مراسلة ][ عرض الرسالة]؛ بلا «تنبيه جديد»؛ لا معرفات/روابط ظاهرة (الرابط داخل href فقط)؛ حد 4096 مع «…».
  * F: سقف ساعي/يومي، jitter 2–5 ث، تعطيل بث القنوات، ChatWriteForbidden/FloodWait آمنة، التحقق من كيان MentionName بعد الإرسال، عزل hash لكل owner_account.
- التحقق: 911/911 اختبار PASS (بعد الإصلاح)؛ py_compile سليم؛ دفع 2ba57ef → origin/main.

Stage Summary:
- origin/main = 2ba57ef (v11.5.1). خط الأساس موثّق: 911/911 PASS.
- **إجراء مطلوب من المستخدم (إنتاج):** أضف في لوحة Render → Environment → ALERT_BOT_TOKEN=توكن البوت (ALERT_BOT_ENABLED افتراضياً true). بدونها يعمل fallback بلا أزرار. لا أملك RENDER_API_KEY لأضيفها تلقائياً.
- حالة نشر Render: مفعّل بالدفع (autoDeploy) — لم يُتحقق من نجاحه من هنا (لا مفتاح Render) — يُتحقق عبر لوحة Render أو /health/full بعد الإقلاع.
- تأكد أن @BZ711BR_bot عضو (يفضل admin) في TARGET_GROUP_ID الإنتاجي — يُفحص تلقائياً عند الإقلاع ويسجّل تحذيراً.

---
Task ID: 5 (بلاغ الإنتاج: التكرار — حاجز صدى التنبيهات v11.5.2)
Agent: Z.ai Code (main orchestrator)
Task: المستخدم أبلغ أن التنبيه يتكرر بنفس النص وبطاقات متداخلة/فارغة «👤: مستخدم 💬:» — المطلوب: تنبيه واحد فقط ضابط بكل الأزرار

Work Log:
- تشخيص بالقراءة المباشرة (لا لوجات Render متاحة): القالب الموحد الجديد مضغوط بحيث يقع نص التنبيه ضمن حد 6..65 ← المراقبة مفتوحة على كل شيء (sources فارغة) ← رسالة التنبيه في مجموعة الهدف تُرى كرسالة جديدة ← تحوي الكلمة الأصلية ← تنبيه يحوي بطاقة سابقة ← سلسلة صدى (يشرح البطاقات المتداخلة و«مستخدم» الفارغة وتوقفها ذاتياً بعد تجاوز 65).
- ثغرة ثانية مُثبتة في الكود: AlertBot timeout → return False → fallback فوري عبر حساب مستخدم = إعادة إرسال أعمى لنتيجة مجهولة (تنبيه مكرر متطابق).
- الإصلاح v11.5.2:
  1) سجل هوياتنا الذاتية register_self_sender/is_self_sender (بوت التنبيهات عند getMe + كل حساب عند connect عبر get_me) — _validate_event يتخطاها قبل بوابة الطول (stat echo_skipped).
  2) is_alert_card_echo — نص يبدأ «👤:» و«💬:» خلال 64 حرفاً = بطاقة منتجرة يُرفض حتى بمرسل مجهول (طبقة نصية مستقلة عن التسجيل).
  3) unknown_send_result_policy + _verify_alertbot_delivery: timeout/network → تحقق عبر عميل AlertUI (MTProto بهوية البوت — آخر 10 رسائل out=True خلال 180 ث + مطابقة النص الظاهر بعد نزع HTML وفك الترميز)؛ مُتحقق التسليم = لا fallback (contact_method=bot_verified)؛ timeout بلا مُحقّق = لا إعادة إرسال (المواصفة: unknown ≠ confirmed failure)؛ network_error بلا مُحقّق = fallback.
- إحصاء echo_skipped في _stats وget_stats للوحة. BOT_VERSION → 11.5.2.
- التحقق: 32 اختبار جديد (tests/test_echo_guard_v11_5_2.py) + المجموعة كاملة 943/943 PASS. Commit cb6f424 مدفوع → origin/main → نشر Render تلقائي.

Stage Summary:
- سلسلة الصدى مقطوعة من طبقتين مستقلتين (هوية + نص)، وإعادة الإرسال الأعمى للنتيجة المجهولة ممنوعة مع تحقق تسليم حقيقي عند توفر AlertUI.
- تنبيه واحد فقط، عبر AlertBot (T1) بكل الأزرار؛ F يستمر تحت التنبيه كرد نصي مقصود.
- صفر تغيير على: الكلمات/التصنيف/الحد 6..65 نفسه/dedup L1-L3/الطوابير/Render/الجلسات.

---
Task ID: 7 (تحليل لوج Render الإنتاجي — v11.5.3 تفعيل عميل أزرار التنبيهات)
Agent: Z.ai Code (main orchestrator)
Task: المستخدم أرسل لوج نشر v11.5.2 الإنتاجي — تحليله وإصلاح ما يكشفه

Work Log:
- النشر نجح والإصدار الحي. قراءة اللوج سطراً سطراً:
  * 6/7 حسابات متصلة — Account 2 (+9665••••••••(مُلطّف)) جلسته منتهية (Session expired) → يتطلب إعادة دخول من /login (إجراء للمستخدم).
  * بوت التنبيهات الإنتاجي = @alzariqi711r_bot (id=8433129002) عضو administrator في قناة الهدف — التوكن البديل @BZ711BR_bot الذي أرسله المستخدم غير مطلوب في Render (المتغير مضبوط أصلاً بقيمة أخرى).
  * ملاحظة تشغيلية: antispam يعمل (WATCH LIST + SPAM CONFIRMED) — نشاع طبيعي لا أعطال؛ لا آثار صدى بعد v11.5.2 في المقطع.
  * تحذير EPHEMERAL STORAGE معروف: SQLite بلا DATABASE_URL — البيانات تُفقد عند إعادة النشر (يوصى PostgreSQL لاحقاً).
- خلل مكتشف من اللوج وأُصلح (v11.5.3): «api_id/api_hash مفقود — عميل أزرار التنبيهات معطّل» — سببه الاعتماد حصراً على حساب is_main غير الموجود في الإنتاج. الإصلاح: _pick_alertui_account (is_main أولاً ثم أول حساب متصل له اعتماديات — نفس اختيار Fallback main client). تفعيل العميل يُشغّل معالجات أزرار التنبيهات + طبقة الإرسال الاحتياطي بالأزرار + تحقق تسليم timeout (v11.5.2).
- التحقق: 4 اختبارات جديدة للاختيار + المجموعة كاملة 947/947 PASS. Commit 68ecaac مدفوع → origin/main → نشر Render تلقائي.

Stage Summary:
- v11.5.3 في الطريق للإنتاج. بعد إقلاعه يُتوقع في اللوج: «AlertUI creds from connected account: Account 1» بدل تحذير api_id/api_hash مفقود، وزرّا التنبيه يعملان عبر AlertBot Bot API (URL buttons) دوماً، ومع العميل الحي صارت طبقة الزرار الاحتياطية والتحقق فعالتين.
- مطلوب من المستخدم: إعادة دخول Account 2 عبر /login.

---

Task ID: 9 (المهمة الهندسية: إصلاح شامل وموثوق لقالب التنبيهات وروابط المرسل والأزرار — v11.6.0)
Agent: Z.ai Code (main orchestrator)
Task: فحص مسار التنبيه كاملاً، توحيد القالب «المرسل: الاسم — @username / نص الرسالة:» بلا رموز وبلا blockquote، ضمان لوحة أزرار فعلية [ عرض الرسالة ][ مراسلة ] في كل مسارات الإرسال، واختبار كل شيء

Work Log:
- sandbox أُعيد إنشاؤه: استنساخ عام للمستودع (origin/main=6fa0635=v11.5.3) + بناء .venv (uv, Telethon 1.45).
- خريطة المسار (مثبتة بالكود): _validate_event (بوابة 6..65 على النص الأصلي فقط) ← _analyze_and_alert ← _send_alert (dedup ← rate limiter ← similarity ← nav resolver ← ContactTarget ← _build_alert) ← AlertBot.send (Bot API) ← AlertBotClient (MTProto) ← سلّم حسابات المستخدمين (L1 username / L2-3 mention / L4 no-mention + forward) ← DLQ retry.
- جذور فقدان الأزرار المُثبتة: (1) _build_alert كان يعيد buttons=None دائماً لمسار الحسابات؛ (2) GENERIC_BUTTON_ERRORS كان يعيد الإرسال بلا أزرار ويحسبه نجاحاً.
- contact_target.py (المصدر الوحيد): SENDER_LABEL="المرسل: "، MSG_TEXT_LABEL="نص الرسالة:"، build_username_suffix() (« — @username» فقط عند صلاحية الشكل)، build_alert_entity_pack بالقالب الجديد بلا blockquote (كيان TextUrl على الاسم فقط، offset محسوب من الترويسة).
- alert_bot.py: build_alert_html بالقالب الحرفي (الترويسة + « — @username» مهربة + قسم النص بلا إطار، المهروب HTML فقط؛ حد 4096 الوحيد للقص)؛ build_alert_buttons بالترتيب الحرفي [ عرض الرسالة ][ مراسلة ]؛ رفض زر tg://user → إسقاط مراسلة فقط؛ رفض عام → إعادة بلوحة t.me فقط ثم فشل مؤكد للسلّم (لا نصية-بلا-أزرار كنجاح — §5.7).
- monitors.py: حارس الصدى بالتوقيع الجديد (المرسل: + نص الرسالة: خلال 256) مع إبقاء توقيع 👤/💬 القديم؛ _verify_alertbot_delivery يقبل الترويسين؛ _build_alert يعيد اللوحة الحقيقية؛ _send_alert_payload يحاول الأزرار في مسار الحسابات مع تدهور صادق (كشف خطأ button/markup → إعادة بلا أزرار + عداد user_buttons_dropped + قفل لكل حساب؛ والرفض الصامت يُكشف بفحص reply_markup في المرسَل).
- alert_ui.py: رفض لوحة → إعادة بلوحة t.me فقط قبل النسخة النصية (أخير منفذ موثق) مع عداد.
- config.py: BOT_VERSION=11.6.0؛ شارة اللوحة BR7 · v11.6؛ حدّث توثيق رؤوس الملفات.
- الاختبارات: تحديث عقود 7 ملفات قديمة (GOLDEN/offsets/ترتيب الأزرار/رد فشل عام) + ملف جديد tests/test_alert_template_v11_6.py بمصفوفة الحالات الـ14 الإلزامية كاملة (26 اختباراً). **974/974 PASS**.
- فحص الفرق: 16 ملفاً (+1078/−275) — بلا أسرار/جلسات/توكنات (scan سالب). Commit محلي: ec73f6d.

Stage Summary:
- القالب الجديد مؤكد بالاختبارات: «المرسل: الاسم — @username» (الاسم وحده رابط) ← (فارغ) ← «نص الرسالة:» ← (فارغ) ← النص الأصلي كاملاً مهرباً HTML؛ لوحة فعلية أسفل التنبيه في كل المسارات (Bot API / AlertBotClient / حسابات المستخدمين).
- الحدود الموثقة بصدق: مراسلة تُسقط فقط عند رفض tg://user (خصوصية تيليجرام) أو بلا هدف؛ عرض يختفي بلا رابط حقيقي (لا روابط وهمية)؛ مسار الحسابات قد يرفض الخادم لوحته → تدهور مسجل بعداد ولوج (قيد منصة).
- **لم يُنفذ بعد (بيئة بلا توكنات)**: git push (لا PAT في هذا الـsandbox) — أمر الرفع: ضع التوكن ثم `git push origin main` من /home/z/Telegram_BR7 (commit ec73f6d جاهز) → نشر Render تلقائي؛ واختبار Telegram الحقيقي عبر scripts/test_alert_live.py (ALERT_BOT_TOKEN + TARGET_GROUP_ID) — يتحقق من كيان الاسم واللوحة من استجابة sendMessage فعلياً.

---
Task ID: 10 (توكنات المستخدم — الدفع + الاختبار الحقيقي في Telegram v11.6.0)
Agent: Z.ai Code (main orchestrator)
Task: دفع commits v11.6.0 المعلقة + تنفيذ الاختبار الحقيقي بالتوكنين المقدَّمين

Work Log:
- حفظ التوكنين في /home/z/.secrets/ (خارج المستودع، صلاحيات 600) — لا أسرار في أي ملف متتبع (فحص diff قبل الدفع: نظيف).
- تحقق التوكنين: GitHub (Bashar711-rgit، push على Telegram_BR7) + البوت getMe = @BZ711BR_bot (id=7886937106).
- خط الأساس أعيد تشغيله على الآلة الحالية: **974/974 PASS** (collect=974، exit=0).
- **الدفع**: 6fa0635..2d9f7b0 (ec73f6d القالب + 2d9f7b0 worklog) → origin/main → نشر Render التلقائي مفعّل. ثم a1a7b83 (سكربتات الاختبار الحي).
- **اختبار Telegram الحقيقي** (قناة الاختبار B_Bot = -1002707889921 حيث البوت مدير):
  * probes (P1..P4، حُذفت فوراً): كيان tg://user?id **يُسقط دائماً** من Bot API حتى بمعرف حقيقي يعرفه البوت؛ زر tg://user?id بمعرف حقيقي **مقبول**؛ بمعرف غير موجود → **BUTTON_USER_INVALID يرفض الرسالة كلها**.
  * الحالة A (مع username): tier=T1 name=N1 contact=B1 view=V2 buttons=2/2 → وصل.
  * الحالة B (بدون username، معرف حقيقي): الكيان سُقط → الإصلاح الذاتي عمل (healed=source_message_link) → tier=T1 name=N2 contact=B4 buttons=2/2 → وصل.
  * الحالة C (معرف وهمي → رفض الخادم): السلم اكتشف BUTTON_USER_INVALID وأعاد بلا زر «مراسلة» → وصل ok=True tier=T2 (زُر العرض بقي).
  * الحالة D (لا رابط رسالة): وصل ok=True buttons=1/2 — «عرض الرسالة» يختفي بصدق.
  * 4 رسائل حية متروكة في قناة B_Bot لفحص الأزرار يدوياً من عميل تيليجرام (لا سكربت يستطيع النقر).
- ملاحظة توثيق: سطر [ALERT] يعرض buttons=2/2 (اللوحة المبنية)؛ في الحالة C المسلَّم فعلياً 1/2 — tier=T2 هو المؤشر الصادق.

Stage Summary:
- **v11.6.0 مدفوع ومُختبَر حياً**: القالب/الهروب/اللوحة/السلم كلها مثبتة عبر API الحقيقي، والإصلاح الذاتي وسلم BUTTON_USER_INVALID يؤكدان سلوكهما في الواقع.
- اكتشاف حقيقي موثق: كيان tg://user لا يبقى أبداً في مسار Bot API (التدهور الصادق: اسم نصي + إصلاح ذاتي برابط الرسالة)؛ زر tg:// يعمل بالمعرفات الحقيقية فقط والسلم يحمي من المحذوف.
- لم يتحقق من هنا: شارة Render (لا عنوان خدمة ولا RENDER_API_KEY في البيئة) — التحقق من الشارة BR7 · v11.6.0 أو /health على المستخدم/اللوحة.

---
Task ID: 11 (البروتوكول الهندسي الذاتي الشامل — مراجعة 3 وكلاء + إصلاحات v11.7.0)
Agent: Z.ai Code (main orchestrator)
Task: فحص شامل لكامل المستودع، تشخيص بالأدلة، إصلاح العيوب المؤكدة، اختبارات انحدار، دفع

Work Log:
- G0-G3: بيئة نظيفة (HEAD=f97f87e متزامن)، جرد آلي كامل (98 ملفاً/76 py/26,429 سطر جذري/12,993 اختبار)، محاكاة إقلاع نظيفة PASS، خط أساس 974/974 PASS.
- اكتشاف إنتاج: telegram-bot.onrender.com هو مثال تعليقات فقط (الخدمة عليه معلقة لغيرنا) → التحقق الإنتاجي محجوب (لا عنوان حقيقي ولا RENDER_API_KEY).
- 3 وكلاء مراجعة موازية: الاستقرار/الغير متزامن، قاعدة البيانات، الفلترة/منع التكرار — بكل واحد إعادة إنتاج حية للعيوب.
- العيوب الحرجة المُثبتة والمُصلَحة: A-1 إعادة اتصال DB تقتل مهمة الكاتب نفسها (زومبي أبدي بعد أي انقطاع عابر)؛ A-2 /health دائماً 200 (ميت=سليم لـRender)؛ A-4 DLQ يتجاهل FloodWait (تنبيهات تموت خلال الفيضان)؛ A-3 انتهاء الجلسة يولد حلقة إعادة اتصال لا نهائية؛ F-1 بوابة التشابه تكبت تنبيهات حقيقية (0.977 لرسالتين تختلفان برقم!) + نافذة منزلقة بلا نهاية؛ D-1/D-2/D-4/D-5 توافق PG مكسور كلياً (DATETIME عارية/MAX قياسية/fail-fast/OR IGNORE)؛ D-3 عشرة جداول intel بلا أي احتفاظ؛ F-5 إهمال dialekt يعطّل كلمات معلنة (مره/تورطت)؛ F-6 النفي يكبت «لا، ابي حل واجب»؛ F-2/F-3 قانون مزدوج + معهد/اكاديميه في hard_signals.
- إصلاحات ثانوية: مهلة 15s لحل الكيان، DLQ لانهيار الـHandler (بحارس منع التكرار)، حصر كاش can_send_alert، متطلبات اختبار معلنة، requirements.lock حقيقي (كان وهمياً)، تلطيف PII (هاتف إنتاجي) من worklog.
- اختبارات: tests/test_audit_v11_7.py — 24 اختبار انحدار جديد يغطي كل إصلاح (شاملة مصيدة REAL<TEXT في المقارنات). **998/998 PASS** (كان 974).
- أثناء الاختبار: اكتُشف أن monitors.py لا يستورد re على مستوى الملف → NameError مبتلع كان سيُعطّل استخراج FloodWait — نمط الاستيراد المحلي المعتمد طُبق.
- Git: commit 2fafec9 دُفع (f97f87e..2fafec9) → Render autoDeploy. فحص أسرار النهاية: نظيف (سطر الحذف الوحيد هو إزالة الهاتف نفسه).

Stage Summary:
- v11.7.0: أصلع نظام إعادة اتصال DB، /health صادق (503)، DLQ يفهم الفيضان، جلسات ميتة تُعلّق مع إشعار إداري، بوابة تشابه عادلة، توافق PG حقيقي، احتفاظ intel، فلترة أدق (3 فئات FN مثبتة أُصلحت).
- حدود موثقة: باب 65 حرفاً هو مواصفة مستخدم سابقة — لم يُمس؛ لوحتا الإدارة مقصودتان؛ لا تغييرات Schema؛ لا اعتماديات جديدة إنتاجية.
- التحقق الإنتاجي (Render/الشارة/اللوج) محجوب من البيئة — يحتاج عنوان الخدمة أو موافقة مستخدم من لوحة Render.

---
Task ID: 15 (v11.7.1 إعادة بناء كاملة + أول دفع ناجح — بيئة أعيد تعيينها)
Agent: Z.ai Code (main orchestrator)
Task: البيئة أُعيد تعيينها وفُقدت الالتزامات المحلية غير المدفوعة (d316780→7424465) — إعادة بناء المهمة كاملة من الأدلة ودفعها فوراً

Work Log:
- استنساخ جديد عند 9c3a3f0 (v11.7.0) بتوكن المالك (متغير بيئة لحظي فقط — لا أسرار في المستودع).
- خط الأساس: 997+1 فشل بيئي وحيد (test_local_git_state_failsafe يثبّت فرع main) → أُصلح يقارن بالفرع الفعلي (subprocess rev-parse).
- إعادة بناء v11.7.1 كاملة: message_context.py جديد (جامع + ذاكرة tg://user TTL 6h + 3 دوال وجهات + context_from_contact_target)؛ antispam (observe allow لـsid=0، confirm_spam(0) مرفوض، setup يتجاهل صفوف 0، شرط (هـ) خلف إعداد ANTISPAM_CONFIRM_SPAM_INDICATOR_REQUIRED الافتراضي المغلق)؛ alert_bot (text_mention مقبول، استيراد الذاكرة، pre-heal قبل أول إرسال، تعليم الذاكرة عند الرفض، _sync_built_buttons للعدّ الفعلي، context= fill-if-absent، «مرسل غير محدد»)؛ reply_fix (ladder + on_tier_fail مع بقاء المسار القديم حرفياً)؛ monitors (جمع السياق مرة في _send_alert → _build_alert → AlertBot.send، سلّم الرد التعويضي من _resolve_mention_input_users داخل المهمة الخلفية مع mention_negative، الاسم الاحتياطي)؛ contact_target («مرسل غير محدد»)؛ config (BOT_VERSION 11.7.1).
- تصحيحات صدق أثناء البناء: await get_sender الحي (كان coroutine غير منتظر)؛ عزل حقل sender_usernames (فشله أسقط الجامع كله)؛ account_language NOT_APPLICABLE (لا اسم حساب كيُقرأ لغة).
- اختبارات: test_v11_7_1_regressions (14)، test_message_context (47)، test_antispam_edges (12)، conftest autouse reset. دوْر إثبات فشل على القديم (worktree) مؤجل بعد الدفع — الأولوية حماية العمل.
- المجموعة الكاملة: 1071/1071 ثم 1072/1072 بعد إضافة db_writer (/health/full: writer_running/writer_queue_size/dead_letters_total/unresolved + get_dead_letter_counts في database.py + اختبار). exit=0 في التشغيلين.
- **الدفع الأول ناجح ومُتحقق**: v11.7.1-engineering=1ceb731 على origin (فشل سابق كان متغير بيئة فارغاً فقط — التوكن سليم بصلاحية Contents write مثبتة بـ422 وليس 401).

Stage Summary:
- v11.7.1 مدفوعة إلى GitHub على فرع منفصل: جامع مركزي + إصلاحات الإنتاج الخمسة + 74 اختباراً جديداً.
- التالي: PR → دمج → Render deploy → تحقق /health.

---
Task ID: 16 (النشر الإنتاجي — PR #1 مدموج + Render live + شارة الإصدار)
Agent: Z.ai Code (main orchestrator)
Task: PR → دمج → نشر Render → تحقق حي

Work Log:
- PR #1 أُنشئ (v11.7.1-engineering → main) **ودُمج بنجاح**: main=78ca82e (merge commit).
- Render API (بادئة rnd_ صغيرة الحرف — المفتاح المرسل بـRnd_ رُفض 401): الخدمة srv-daau2me7bikc73ccfffg "Telegram_BR7" — النشر التلقائي انطلق فور الدمج: dep-dbehsbe7bikc73b9vkr0 commit=78ca82ec0 → **live خلال ~70 ثانية** (السابق live كان 9c3a3f061 v11.7.0 — يؤكد ما وثق سابقاً).
- تحقق حي: /health → HTTP 200 {"status":"ok","database":"ok","db_healthy":true} مرتين — عقد 503 محفوظ.
- اكتشاف حي: شارة اللوحة "BR7 · v11.6" نص ثابت مهجور في templates/dashboard.html (جلسة v11.7.0 أغفلها وسجلها يشير لذلك) → إصلاح: حقن BOT_VERSION الفعلي عند تحميل القالب + اختبار test_index_badge_shows_bot_version. المجموعة: 1073/1073.
- هذا الالتزام + PR #2 لدفع إصلاح الشارة.

Stage Summary:
- v11.7.1 حية على الإنتاج (78ca82ec0) وصحتها خضراء — يتبقى دمج إصلاح الشارة.

---
Task ID: 17 (تحقق نهائي حي — النسخة v11.7.1 كاملة على الإنتاج)
Agent: Z.ai Code (main orchestrator)

Work Log:
- النشر الثاني (0abd5ac — إصلاح الشارة) live خلال ~30 ثانية: dep-dbehv3u7bikc73ba32rg.
- التحقق الحي النهائي: /health → HTTP 200 {"status":"ok","database":"ok","db_healthy":true}؛ شارة اللوحة الحية: **BR7 · v11.7.1** (بعد تبديل المثيل خلف موازن الحمل — تأخير ~45 ث طبيعي).
- خط الأنابيب الكامل مكتمل ومثبت بالأدلة: إعادة بناء → 1073/1073 → دفع → PR #1+#2 → دمج → Render autoDeploy → live → فحص صحة حي.

Stage Summary:
- v11.7.1 على الإنتاج بالكامل: main=0abd5ac، الخدمة live، الصحة خضراء، الشارة صادقة.
- ملاحظة أمان للمالك: التوكنان شاركا في المحادثة — **يُنصح بتدويرهما** (GitHub: Settings → Developer settings → Fine-grained tokens؛ Render: Account Settings → API Keys) بعد قراءة هذا السجل.

---
Task ID: 18 (v12.1.0 — التزامن الصلب + محرك الفلترة الموحد + صيغة الصورة المرجعية)
Agent: Z.ai Code (main orchestrator)
Task: حل مشكلة التنبيهات نهائياً — لا فقدان، لا تكرار، لا تنبيه مشوّه، صيغة مطابقة للصورة المرفقة

Work Log:
- **تشخيص جذري (بأدلة)**:
  * ثغرة فقد (1): pop_from_queue كانت SELECT ثم DELETE في نفس اللحظة — أي انهيار
    (SIGKILL/OOM/نشر Render) بين السحب واكتمال المعالجة كان يُفقد الحدث للأبد.
  * ثغرة فقد (2): قياسات الطول غير الموحدة — _validate_event تقيس المقصوص بينما
    _store_message والفلتر يقيسان الخام: رسالة خامها 80 ومقصوصها 40 كانت تعبر
    التحقق ثم تُقتل "no_content"/"too_long" → طلب صالح بلا تنبيه أبداً.
  * صيغة البطاقة: اللقطة المرجعية من المستخدم تُظهر الاسم و@username كلاهما
    رابطاً أزرق — الكود كان يربط الاسم فقط ويترك @username نصاً عادياً.
- **التزامن الصلب (claim/ack/recover — at-least-once)**:
  * processing_queue يكسب status/claimed_at/attempts (ترحيل إضافي idempotent
    للـSQLite عبر _migrate_queue_inflight وPG عبر ADD COLUMN IF NOT EXISTS +
    migrations/0003_queue_inflight.sql — طُبّق فعلياً على Supabase الحية).
  * pop_from_queue أصبح claim بلا حذف + حقن _queue_id؛ ack_queue_event يحذف
    بعد اكتمال المعالجة (main.py worker)؛ recover_stale_inflight يسترد العالق
    (claimed_at > QUEUE_INFLIGHT_MAX_AGE_SECONDS=600s) بوسم _recovered=1 أو
    يودعه dead_letters بصدق بعد QUEUE_INFLIGHT_MAX_ATTEMPTS=3 محاولات.
  * حلقة استرداد دورية _inflight_recovery_loop (كل 120s) + استرداد إقلاعي عبر
    run_startup_maintenance.
  * مسار المسترد في _process_event_pipeline: رسالة مخزنة + إيصال تنبيه
    (alert_exists على جدول alerts) → تخطٍّ صادق؛ بلا إيصال → إعادة إرسال
    (فك حجز dedup المحاولة المنهارة أولاً في _send_alert) — at-least-once
    موثق مقابل فقدان كلي، وطبقات dedup اللاحقة تحكم الحالات الطبيعية.
- **محرك الفلترة الموحد**: unified_alerts/filter_policy.py — مصدر حقيقة واحد
  (strip + صفر-العرض + 6..65 شاملاً الحدين، CFG حي) يُستخدم من _validate_event
  و_store_message وfilter_engine.analyze — الثغرتان مغلقتان من جذورهما،
  والفشل-آمن يحفظ السلوك السابق عند أي خلل.
- **صيغة الصورة المرجعية (v12.1.0)**: build_username_suffix_linked في
  contact_target — «المرسل: {الاسم رابط} — {@username رابط}» في قالب Bot API،
  وكيان MessageEntityTextUrl ثانٍ في مسار MTProto (build_alert_entity_pack)
  بنفس الإزاحة المحسوبة. الأزرار [عرض الرسالة][مراسلة] كما هي (مطابقة).
- **العدادات الصادقة**: unified_stats تكسب filter_ok/too_short/too_long/invalid,
  queue_recovered/recovered_dead, recovered_resend/recovered_dupe_skip,
  inflight_errors, policy_errors — تظهر في /health/full (مفاتيح ثابتة).
- **الإصدار**: BOT_VERSION 12.1.0.
- **الاختبارات**: 27 اختباراً جديداً (tests/test_unified_v12_1.py) تغطي السياسة
  الموحدة وclaim/ack/recover والإيصال وصيغة البطاقة وثغرة الطول؛ تحديث توقعات
  القوالب الذهبية لصيغة الصيغة المرجعية الجديدة (GOLDEN S1/S4، acceptance_1،
  case1، send_username_method، nav_resolver frozen-builder)؛ توحيد عقد الفلتر
  مع المواصفة (نصوص < 6 خارج ظرف المحرك). الملفات الحرجة: 195 passed.
- **قاعدة البيانات الفعلية (Supabase)**: فحص حي — 30 جدولاً، 321 تنبيهاً،
  32,535 رسالة؛ ترحيل 0003 طُبّق فعلياً (status/claimed_at/attempts + فهارس).

---

Task ID: 19 (v12.2.0 — العقد الهندسي الرئيسي: تدقيق كامل + إصلاح أمني S-1 + مرونة فلترة F-14)
Agent: Z.ai Code (main orchestrator)
Task: تنفيذ دورة التدقيق الكاملة على v12.1.0 (خط الأساس) وإصلاح ما ثبت أثره باختبارات فعلية

Work Log:
- خط الأساس: v12.1.0 (c518e0a) متزامن مع origin، شجرة نظيفة. **1133/1133 PASS** بعد استكمال
  4 اعتماديات ناقصة في venv المضيف (langdetect/pyarabic/uvloop/itsdangerous — كلها في
  requirements.txt؛ فشل الـ12 الظاهري كان بيئياً وأثبت نمطين مكشوفين لغياب تبعية).
- **S-1 (أمن — أُصلح)**: dashboard._AuthGuard.client_ip كانت تأكم أول قيمة XFF (يملكها
  المهاجم: ترويش قفل brute-force + سَمّ ضحية). المُحلّل الموحد security.resolve_client_ip
  (نظير غير موثوق→تجاهل XFF كلياً؛ وإلا أقصى يمين غير موثوق) استهلكه dashboard + webadmin
  معاً؛ حذف النسخة المكررة من webadmin؛ TRUSTED_PROXIES موثق في render.yaml. 17 اختباراً
  جديداً (tests/test_client_ip_security.py) — اختبارا الترويش والسَمّ يفشلان على القديم.
  حادثة موثقة: أول تشغيل فشل بسبب غياب global _proxy_cache + import time في security.py
  (الفشل-الآمن كتمه) — أُصلح والاختبارات تثبت المسار السليم فعلاً الآن.
- **F-14 (موثوقية — أُصلح)**: غياب pyarabic كان يُسقط إزالة التشكيل/التطويل بصمت (FN).
  مسار احتياطي regex بنفس دلالة araby + اختبار يفرض غياب التبعية.
- **S-3**: 5 F821 (تعليقات أنواع) أُصلحت — deque صريح في monitors + Dict في maintenance.
- **D-1 (مفتوح — قرار مالك)**: مخطط br7 (44 جدولاً) على Supabase موازٍ غير مستخدم بلا
  صلاحيات لـbr7_app؛ التطبيق الحي على public (alerts=336، messages=34012، alert_dedup=77،
  dead_letters=0، queue=6) — **ترحيل 0003 (status/claimed_at/attempts) مُتحقق حياً ✓**.
- **S-4/W-1/W-2/V12-D**: موثقة في FINDINGS_REGISTER بلا تغيير كود (نطاق مقيد بالثابت).
- نظافة: إصلاح تحذير pytest (علامة asyncio على اختبار متزامن في test_features.py).
- BOT_VERSION → 12.2.0 (الشارة تُحقن ديناميكياً).
- **الوثائق الـ13 كاملة**: docs/audit/{BASELINE,FINDINGS_REGISTER,TEST_REPORT,REMEDIATION_LOG,
  SECURITY_AUDIT,DATABASE_AUDIT,FUNCTION_AUDIT}.md + docs/architecture/{ARCHITECTURE,
  FILE_INVENTORY}.md + docs/development/DEVELOPMENT_PLAN.md + docs/deployment/
  {DEPLOYMENT_GUIDE,RELEASE_CHECKLIST}.md + docs/operations/RECOVERY_AND_BACKUP.md.
- المجموعة النهائية: **1151/1151 PASS** (EXIT=0) — py_compile كامل PASS.

Stage Summary:
- الالتزام محلي (لا دفع — §25 يتطلب تصريح المالك للدفع إلى main → Render autoDeploy).
- مطلوب من المالك بعد الدفع: تأكيد /health + شارة BR7·v12.2.0 + قرارات D-1 (المخطط) وتدوير الأسرار.
- لم يُختبَر حياً: تيليجرام إنتاجي (ممنوع §2) — كل تغطية الإرسال محاكاة.

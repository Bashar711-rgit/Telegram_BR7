
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

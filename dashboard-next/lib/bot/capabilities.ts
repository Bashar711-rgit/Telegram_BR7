/**
 * Capability manifest — the machine-readable truth of what is LIVE vs CONTRACT.
 * Rendered by /bot overview and /bot/commands; keeps the UI honest by construction.
 */

export type Capability = {
  id: string;
  area: string;
  title: string;
  mode: 'live' | 'live_persistent' | 'contract';
  mechanism?: string;
  depends?: string;
};

export const CAPABILITIES: Capability[] = [
  // Engine control
  { id: 'engine.status', area: 'control', title: 'حالة المحرك الحية (Running/Degraded/Stopped/Starting/Error)', mode: 'live', mechanism: '/health + /health/full + Render service — اشتقاق لحظي' },
  { id: 'engine.restart', area: 'control', title: 'إعادة تشغيل المحرك', mode: 'live', mechanism: 'Render restart endpoint' },
  { id: 'engine.pause_filtering', area: 'control', title: 'إيقاف المعالجة (فوري + مثابر)', mode: 'live', mechanism: 'dashboard:ops + تطبيق فوري على العملية الحارة عبر /service/bot/ops (§3.1 حي v17.2.0)' },
  { id: 'engine.resume_filtering', area: 'control', title: 'استئناف المعالجة (فوري + مثابر)', mode: 'live', mechanism: 'dashboard:ops=false + تطبيق فوري (§3.1 حي)' },
  { id: 'engine.emergency_stop_send', area: 'control', title: 'إيقاف الإرسال طوارئ (فوري + مثابر)', mode: 'live', mechanism: 'dashboard:ops.alerts_paused + تطبيق فوري (§3.1 حي)' },
  { id: 'engine.resume_send', area: 'control', title: 'استئناف الإرسال (فوري + مثابر)', mode: 'live', mechanism: 'dashboard:ops.alerts_paused=false + تطبيق فوري (§3.1 حي)' },
  { id: 'engine.start_stop', area: 'control', title: 'Start / Stop فوري للمحرك', mode: 'contract', depends: 'منفذ أوامر Render (§3.1 المتبقي) — لا إيقاف خدمة Render كلياً؛ البديل الحي: إيقاف المعالجة/الإرسال أعلاه' },
  { id: 'engine.maintenance', area: 'control', title: 'وضع الصيانة الفوري', mode: 'live', mechanism: 'maintenance_on/off = إيقاف فوري + مثابر للمعالجة (§3.1 حي v17.2.0)' },
  { id: 'engine.reload_config', area: 'control', title: 'إعادة تحميل سجل الكلمات على العملية الحارة', mode: 'live', mechanism: 'POST /service/keywords/reload → reload_keywords + FastGate rebuild (§3.2 حي v17.2.0)' },

  // Accounts
  { id: 'accounts.read', area: 'accounts', title: 'قراءة الحسابات وتفاصيلها الكاملة', mode: 'live', mechanism: 'panel_accounts (منح قائم)' },
  { id: 'accounts.update', area: 'accounts', title: 'تعديل الاسم/الأولوية/الحالة/الحساب الرئيسي', mode: 'live', mechanism: 'PATCH panel_accounts (منح قائم)' },
  { id: 'accounts.link', area: 'accounts', title: 'إضافة حساب (هاتف→OTP→2FA→جلسة مشفرة) + تفعيل فوري', mode: 'live', mechanism: 'تدفق Telethon الحقيقي (link_manager) عبر بوابة الخدمة /service/accounts — سر مشترك §3.1 — الجلسة Fernet دائمة' },
  { id: 'accounts.toggle_hot', area: 'accounts', title: 'تفعيل/إيقاف المراقبة فوراً على المحرك (spawn/stop)', mode: 'live', mechanism: '/service/accounts/row/toggle عبر بوابة الخدمة' },
  { id: 'accounts.delete', area: 'accounts', title: 'حذف حساب (super_admin) + إيقاف مراقبه', mode: 'live', mechanism: 'مسار مزدوج من اللوحة: المحرك أولاً (إيقاف فوري + حماية آخر حساب نشط) وإذا نائم المحرك حذف مباشر من panel_accounts في Supabase (المصدر الموثوق) — يعمل دائماً من اللوحة' },
  { id: 'accounts.test_reauth', area: 'accounts', title: 'اختبار اتصال دوري مجدول/إعادة مصادقة آلية', mode: 'contract', depends: '§3.3 المتبقي — الإصلاح اليدوي متاح: حذف ثم إعادة إضافة' },

  // Infrastructure — كل الإعدادات من اللوحة (حفظ مزدوج Render + Supabase)
  { id: 'infra.env_read', area: 'infra', title: 'قراءة متغيرات بيئة Render من اللوحة (مموّهة دائماً)', mode: 'live', mechanism: 'Render API /env-vars + حالة المرآة' },
  { id: 'infra.env_write', area: 'infra', title: 'إضافة/تعديل/حذف متغيرات env من اللوحة — حفظ مزدوج', mode: 'live_persistent', mechanism: 'Render API (مصدر التشغيل) + مرآة AES-256-GCM في app_settings (Supabase) + نشر تلقائي' },
  { id: 'infra.env_restore', area: 'infra', title: 'استعادة متغير env من مرآة Supabase إلى Render (علاج drift/حذف خاطئ)', mode: 'live', mechanism: 'mirrorGetPlain خادمياً فقط + upsertEnvVar + نشر تلقائي' },
  { id: 'infra.env_sessions', area: 'infra', title: 'إدارة جلسات الحسابات عبر env (ACCOUNT_*/MAIN_SESSION_STRING) من اللوحة', mode: 'live_persistent', mechanism: 'نفس حفظ env المزدوج — الطريقة المفضلة تبقى معالج الربط الحي' },
  { id: 'infra.deploy', area: 'infra', title: 'تشغيل نشر Render من اللوحة (مع/بلا تفريغ الكاش)', mode: 'live', mechanism: 'Render API /deploys' },
  { id: 'infra.restart', area: 'infra', title: 'إعادة تشغيل الخدمة من اللوحة', mode: 'live', mechanism: 'restart endpoint أو deploy fallback' },

  // Sync — طبقة المزامنة (متطلب §5 من عقد المالك)
  { id: 'sync.idempotency', area: 'sync', title: 'مفاتيح عدم التكرار للعمليات الحساسة (x-idempotency-key — إعادة الطلب تعيد نفس النتيجة بلا تنفيذ ثانٍ)', mode: 'live', mechanism: 'app_settings[idem:*] TTL 24سا — db_post/patch/delete، env، accounts/row، users، tag، reconcile + أوامر المحرك بلجر مسبق' },
  { id: 'sync.reconcile', area: 'sync', title: 'تسوية DB↔المحرك: فجوات المراقبين والأيتام وانحراف المرآة + إصلاح أيتام idempotent', mode: 'live', mechanism: 'POST /api/bot/reconcile — monitor_detail v17.1.0 (row_id/phone) + /service/accounts/row/stop' },
  { id: 'sync.sse', area: 'sync', title: 'بث حي لحالة المحرك (SSE كل 10 ثوانٍ + إعادة اتصال تلقائية)', mode: 'live', mechanism: 'GET /api/bot/stream — اشتقاق لحظي getBotStatus — صفحة التحكم تعرضه حياً' },
  { id: 'sync.preview_guard', area: 'sync', title: 'فصل البيئات: نشرات المعاينة للقراءة فقط افتراضياً (حارس VERCEL_ENV + شارة مرئية)', mode: 'live', mechanism: 'withApi يرفض أي mutation في preview إلا بـ PREVIEW_ALLOW_MUTATIONS=1 على بيئة المعاينة' },
  { id: 'sync.db_transparency', area: 'sync', title: 'شفافية تخزين المحرك: كشف حي إن كانت قاعدته دائمة (PostgreSQL) أو مؤقتة (SQLite تُمسح عند النشر) + منح قراءة GRANT بقائمة بيضاء', mode: 'live', mechanism: '/service/db/info + /service/db/grant_read + لافتة تحذير حقيقية في /bot/engine' },

  // Sources
  { id: 'sources.crud', area: 'sources', title: 'إضافة/تعديل/حذف/تفعيل المصادر + متعددة + CSV', mode: 'live', mechanism: '/api/db/sources (منح قائم)' },
  { id: 'sources.check', area: 'sources', title: 'فحص وصول المصدر (حي عبر عميل تيليجرام)', mode: 'live', mechanism: 'POST /service/sources/check — وصول الكيان + حالة العضوية + صلاحية الإرسال (§3.4 حي v17.2.0)' },

  // Keywords
  { id: 'keywords.read', area: 'keywords', title: 'قراءة سجل الكلمات الكامل (20 مجموعة)', mode: 'live', mechanism: 'dashboard:keywords_json' },
  { id: 'keywords.write', area: 'keywords', title: 'إضافة/حذف كلمات + بناء حار فوري + كشف التكرار', mode: 'live', mechanism: 'كتابة السجل ثم POST /service/keywords/reload (§3.2 حي v17.2.0) — المحرك نائم: deferred بصدق' },
  { id: 'keywords.categories', area: 'keywords', title: 'تصنيفات جديدة غير المحركية', mode: 'live', mechanism: 'مجموعات custom:* داخل السجل' },

  // Engine analysis
  { id: 'engine.test_bench', area: 'analysis', title: 'مقعد اختبار التحليل برسالة حقيقية (فلتر + مصنّف + بوابة سريعة)', mode: 'live', mechanism: 'POST /service/engine/analyze على العملية الحارة (§3.5 حي v17.2.0)' },
  { id: 'engine.profiles', area: 'analysis', title: 'حفظ/مقارنة/استعادة ملفات الإعدادات', mode: 'live', mechanism: 'app_settings KV (فهي الحفظ) — التطبيق الفوري للكلمات عبر §3.2 حي' },
  { id: 'engine.weights_write', area: 'analysis', title: 'تعديل الأوزان الحارة على المحرك', mode: 'contract', depends: '§3.2 المتبقي + مخطط filter_weights — الأوزان نفسها تعمل عبر reload الكلمات' },

  // Rules
  { id: 'rules.crud', area: 'rules', title: 'CRUD القواعد + أولوية + تفعيل', mode: 'live', mechanism: '/api/db/rules (منح قائم)' },
  { id: 'rules.test', area: 'rules', title: 'اختبار القاعدة على رسالة حقيقية', mode: 'live', mechanism: 'نفس مقعد الاختبار الحي (§3.5) — قارن قرار الفلتر مع شرط القاعدة يدوياً من /bot/quality' },

  // Alerts
  { id: 'alerts.read_stats', area: 'alerts', title: 'قراءة التنبيهات + معدل نجاح الإشعارات + الأخطاء', mode: 'live', mechanism: 'alerts (منح قراءة)' },
  { id: 'alerts.destinations', area: 'alerts', title: 'إدارة وجهات مستقلة (جدول مخصص)', mode: 'contract', depends: 'جدول notification_destinations (§4) — التوجيه الحالي يعمل عبر rules حي' },
  { id: 'alerts.template', area: 'alerts', title: 'تخصيص قالب البطاقة', mode: 'contract', depends: 'قالب البطاقة مجمّد في unified_alerts/card.py — يتطلب عقد تعديل' },
  { id: 'alerts.resend', area: 'alerts', title: 'إعادة إرسال تنبيه', mode: 'contract', depends: '§3.1' },
  { id: 'alerts.block_sender', area: 'alerts', title: 'حظر مرسل من بطاقة التنبيه', mode: 'live', mechanism: 'blocked_senders POST (منح قائم)' },

  // Blocked
  { id: 'blocked.crud', area: 'blocked', title: 'حظر/إلغاء حظر مرسلين + قائمة موثوقين', mode: 'live', mechanism: 'blocked_senders + allowed_entities (منح قائم)' },
  { id: 'blocked.chats_read', area: 'blocked', title: 'قراءة blocked_chats', mode: 'contract', depends: 'منح SELECT (الجدول موجود، المنح غير مضبوط في هذه المرحلة)' },
  { id: 'blocked.temporary', area: 'blocked', title: 'حظر مؤقت بمدة', mode: 'contract', depends: 'عمود expires_at (§4)' },

  // Quality
  { id: 'quality.stats', area: 'quality', title: 'إحصاءات الجودة من القرارات الحقيقية (قبول/رفض/ثقة/أسباب/تكرار)', mode: 'live', mechanism: 'filter_decisions (منح قراءة)' },
  { id: 'quality.precision_recall', area: 'quality', title: 'Precision / Recall / F1', mode: 'live', mechanism: 'يُحسب فقط عند وجود بيانات مرجعية — يعرض «غير متاح» بدونها (لا اختراع)' },
  { id: 'quality.feedback', area: 'quality', title: 'تعليم FP/FN + إعادة تحليل', mode: 'contract', depends: 'message_feedback (§3.6) — عمود feedback موجود في filter_decisions لكن الكتابة غير مصرح بها في هذه المرحلة' },

  // Performance
  { id: 'perf.live', area: 'performance', title: 'مقاييس حية: طابور/معالجة/زمن/uptime/مراقبون/FloodWait', mode: 'live', mechanism: '/health/full' },
  { id: 'perf.system', area: 'performance', title: 'CPU/Memory + processing_queue + dead_letters', mode: 'contract', depends: 'منح قراءة + §3.8' },
  { id: 'perf.job_actions', area: 'performance', title: 'إعادة محاولة مهمة/تنظيف طابور', mode: 'contract', depends: '§3.7' },

  // Settings
  { id: 'settings.kv', area: 'settings', title: 'قراءة/تعديل إعدادات KV (أسرار مموّهة لغير المدراء)', mode: 'live', mechanism: 'app_settings (منح قائم)' },

  // Commands
  { id: 'commands.ledger', area: 'commands', title: 'سجل أوامر بدورة حياة + Idempotency + تدقيق دائم', mode: 'live', mechanism: 'ledger داخلي + audit_logs (الأوامر التنفيذية تنفذ فعلاً فورياً)' },
  { id: 'commands.executor', area: 'commands', title: 'تنفيذ فوري شامل (pause/resume/maintenance/emergency/restart)', mode: 'live', mechanism: 'بوابة الخدمة /service/bot/ops (§3.1 حي v17.2.0) + Render restart — المتبقي start/stop كلي للخدمة عقد مقصود' },

  // Auth
  { id: 'auth.owner_account', area: 'auth', title: 'حساب المالك balghanme@gmail.com (super_admin)', mode: 'live', mechanism: 'Supabase Auth + dashboard_users (أمر المالك)' },
  { id: 'auth.change_password', area: 'auth', title: 'تغيير كلمة المرور الذاتي من اللوحة', mode: 'live', mechanism: 'تحقق كلمة المرور الحالية ثم تحديث عبر service_role + audit' },
];

export function capabilitiesByArea(): Record<string, Capability[]> {
  const out: Record<string, Capability[]> = {};
  for (const c of CAPABILITIES) {
    (out[c.area] ||= []).push(c);
  }
  return out;
}

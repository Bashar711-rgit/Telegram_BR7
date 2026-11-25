import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { getRegistry } from '@/lib/bot/keywords';
import { keywordsHotReload } from '@/lib/bot/engine-gateway';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/**
 * POST /api/bot/keywords/reload — v17.2.0 §3.2 حي.
 * يرسل السجل الفعلي من Supabase إلى المحرك ليثبّته محلياً ويبني فوراً
 * (reload_keywords + FastGate). يفشل بصدق إن كان المحرك غير متاح — لا نجاح وهمي.
 */
export const POST = withApi(
  { name: 'bot_keywords_reload', minRole: 'admin', mutating: true, rate: { limit: 6, windowMs: 60000 } },
  async ({ email, rid, req }) => {
    const reg = await getRegistry();
    const h = await keywordsHotReload(reg.ok ? (reg.registry as Record<string, unknown>) : undefined);
    await logAudit({
      actor: email,
      action: 'keywords_hot_reload',
      objectType: 'app_settings',
      objectId: 'dashboard:keywords_json',
      before: null,
      after: h.ok
        ? { reloaded: h.data?.reloaded ?? null, groups: h.data?.groups ?? null, source: h.data?.source ?? null, fast_gate: h.data?.fast_gate ?? null }
        : { engineStatus: h.status, error: h.error || null },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });
    if (!h.ok) {
      return err(
        h.status === 0 ? `engine_unreachable: ${h.error || 'لا استجابة'}` : `engine_error_${h.status}: ${h.error || ''}`,
        h.status === 0 ? 504 : 502,
      );
    }
    return ok({
      reloaded: h.data?.reloaded ?? false,
      groups: h.data?.groups ?? null,
      source: h.data?.source ?? null,
      fastGate: h.data?.fast_gate ?? null,
      note: 'أُعيد بناء المحرك من السجل الفعلي (Supabase) فوراً على العملية الحارة (reload_keywords + FastGate)',
    });
  },
);


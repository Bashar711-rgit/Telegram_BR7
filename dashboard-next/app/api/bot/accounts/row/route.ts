import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { rowDelete, rowToggle } from '@/lib/bot/accounts-flow';
import { requireService } from '@/lib/supabase';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    row_id: z.number().int().positive(),
    action: z.enum(['delete', 'toggle']),
    enable: z.boolean().optional(),
  })
  .strict();

type EngineRes = { ok?: boolean; enable?: boolean; error?: string };

type DeleteOutcome = {
  dbDelete: 'ok' | 'already_gone';
  monitorStop: 'immediate' | 'already_gone' | 'deferred_engine_asleep';
};

/**
 * POST /api/bot/accounts/row — إدارة صف حساب قائم.
 * - toggle (admin+): تفعيل/إيقاف المراقبة فوراً على الحلقة الرئيسية (spawn/stop)
 *   مع تحديث panel_accounts — لا انتظار لإعادة التشغيل.
 * - delete (super_admin فقط): حذف بمسار مزدوج يعمل دائماً من اللوحة:
 *     1) المحرك أولاً (يوقف المراقب فوراً ويفرض حماية «آخر حساب نشط»)
 *     2) إن كان المحرك نائماً/بعيد المنال → حذف مباشر من panel_accounts في
 *        Supabase (المصدر الموثوق) — المراقب يتوقف عند استيقاظ المحرك.
 *   الحذف لا يلمس جلسات env (ACCOUNT_*) — تُدار من صفحة Render في اللوحة.
 */
export const POST = withApi(
  { name: 'bot_accounts_row', minRole: 'admin', mutating: true, idempotent: true, rate: { limit: 12, windowMs: 60000 } },
  async ({ body, email, role, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { row_id, action } = parsed.data;

    if (action === 'delete' && role !== 'super_admin') {
      return err('forbidden: حذف حساب يتطلب دور super_admin', 403);
    }
    if (action === 'toggle' && typeof parsed.data.enable !== 'boolean') {
      return err('invalid_body: enable مطلوب لعملية toggle', 400);
    }

    if (action === 'toggle') {
      const res = await rowToggle(row_id, Boolean(parsed.data.enable));
      const d = (res.data || {}) as EngineRes;
      const outcome =
        res.status === 0 || res.error ? `unreachable:${res.error || 'timeout'}` : d.ok ? 'ok' : d.error || `engine_${res.status}`;
      await logAudit({
        actor: email,
        action: 'bot_accounts_row_toggle',
        objectType: 'panel_accounts',
        objectId: String(row_id),
        after: { outcome, enable: parsed.data.enable },
        ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
        userAgent: req.headers.get('user-agent') || '',
        requestId: rid,
      });
      if (res.status === 0 || res.error) return err(`engine_unreachable: ${res.error || 'timeout'}`, 504);
      if (!res.data) return err(`engine_error_${res.status}`, res.status >= 400 ? res.status : 502);
      if (d.ok) return ok({ enable: d.enable ?? null });
      return err(d.error || `engine_error_${res.status}`, 400);
    }

    // ── delete: مسار مزدوج ──
    const ip = (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim();
    const ua = req.headers.get('user-agent') || '';

    // 1) لقطة الصف للتدقيق قبل الحذف (المصدر الموثوق Supabase)
    let rowSnapshot: Record<string, unknown> | null = null;
    try {
      const sb = requireService();
      const { data } = await sb
        .from('panel_accounts')
        .select('id, phone, display_name, status, monitoring_enabled')
        .eq('id', row_id)
        .maybeSingle();
      rowSnapshot = data || null;
    } catch {
      // لا نُفشل العملية — المحرك قد يؤكد الوجود لاحقاً
    }

    // 2) المحرك أولاً: إيقاف فوري للمراقب + حماية «آخر حساب نشط»
    const res = await rowDelete(row_id);
    const d = (res.data || {}) as EngineRes;
    const engineUnreachable = res.status === 0 || Boolean(res.error) || !res.data;
    const engineSaysGone = !engineUnreachable && Boolean(d.error && String(d.error).includes('غير موجود'));
    const engineRejected = !engineUnreachable && !d.ok && !engineSaysGone;

    let outcome: DeleteOutcome;
    if (!engineUnreachable && d.ok) {
      outcome = { dbDelete: 'ok', monitorStop: 'immediate' };
    } else if (engineRejected) {
      // حماية المحرك (آخر حساب نشط مثلاً) — تُحترم ولا يُحذف الصف خلسة
      await logAudit({
        actor: email,
        action: 'bot_accounts_row_delete',
        objectType: 'panel_accounts',
        objectId: String(row_id),
        before: rowSnapshot ? { id: row_id, status: rowSnapshot.status, monitoring_enabled: rowSnapshot.monitoring_enabled } : { id: row_id },
        after: { outcome: `engine_rejected: ${d.error || 'unknown'}` },
        ip, userAgent: ua, requestId: rid,
      });
      return err(d.error || 'engine_rejected', 400);
    } else {
      // 3) المحرك نائم/بعيد المنال أو يقول إن الصف غير موجود → حذف مباشر من DB
      try {
        const sb = requireService();
        const { error } = await sb.from('panel_accounts').delete().eq('id', row_id);
        if (error) return err(`db_delete_failed: ${error.message}`, 500);
        outcome = engineSaysGone
          ? { dbDelete: 'already_gone', monitorStop: 'already_gone' }
          : { dbDelete: 'ok', monitorStop: 'deferred_engine_asleep' };
      } catch (e) {
        return err(`db_delete_failed: ${e instanceof Error ? e.message : 'supabase_unavailable'}`, 500);
      }
    }

    await logAudit({
      actor: email,
      action: 'bot_accounts_row_delete',
      objectType: 'panel_accounts',
      objectId: String(row_id),
      before: rowSnapshot ? { id: row_id, status: rowSnapshot.status, monitoring_enabled: rowSnapshot.monitoring_enabled } : { id: row_id },
      after: {
        outcome: 'ok',
        ...outcome,
        engineStatus: engineUnreachable ? `unreachable:${res.error || 'timeout'}` : res.status,
      },
      ip, userAgent: ua, requestId: rid,
    });

    return ok(outcome);
  },
);

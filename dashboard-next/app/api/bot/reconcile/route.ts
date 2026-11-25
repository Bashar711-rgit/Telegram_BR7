import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { reconcile } from '@/lib/bot/reconcile';
import { logAudit } from '@/lib/audit';
import { z } from 'zod';

export const dynamic = 'force-dynamic';

const Body = z.object({ fix: z.boolean().optional() }).strict();

/**
 * POST /api/bot/reconcile — تسوية DB↔المحرك (admin+).
 * يقرأ: panel_accounts (Supabase) + /health/full monitor_detail (v17.1.0 فيه
 * row_id/phone) + أعلام ops + انحراف مرآة env.
 * fix=true: يوقف المراقبين الأيتام (صفوف محذوفة من DB) عبر /service/accounts/row/stop
 * idempotent — لا spawn تلقائي (اتصال Telethon قرار مشغل).
 * كل تشغيل يُسجَّل في audit مع ملخص الفجوات.
 */
export const POST = withApi(
  { name: 'bot_reconcile', minRole: 'admin', idempotent: true, rate: { limit: 6, windowMs: 60000 } },
  async ({ email, body, rid }) => {
    const parsed = Body.safeParse(body ?? {});
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const fix = Boolean(parsed.data.fix);

    const r = await reconcile(fix);
    if (!r.ok || !r.report) return err(r.error || 'reconcile_failed', 500);
    const rep = r.report;

    await logAudit({
      actor: email,
      action: fix ? 'bot_reconcile_fix' : 'bot_reconcile_report',
      objectType: 'reconciliation',
      objectId: rep.at,
      after: {
        panel_rows: rep.panel_rows.length,
        engine_monitors: rep.engine_monitors.length,
        enabled_no_monitor: rep.enabled_no_monitor.length,
        orphan_monitors: rep.orphan_monitors.length,
        mirror_drift: rep.env_mirror_only.length,
        fixes: rep.applied_fixes,
      },
      requestId: rid,
    });
    return ok(rep);
  },
);

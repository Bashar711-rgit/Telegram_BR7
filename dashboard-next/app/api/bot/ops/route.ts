import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { setOpsFlag, readOpsFlags } from '@/lib/bot/engine';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    flag: z.enum(['filtering_paused', 'alerts_paused']),
    value: z.boolean(),
  })
  .strict();

/**
 * POST /api/bot/ops — persistent ops toggle (admin+).
 * Real write into dashboard:ops; the engine hot path applies it at boot/restart.
 * Always audited with masked before/after.
 */
export const POST = withApi(
  { name: 'bot_ops', minRole: 'admin', mutating: true, rate: { limit: 10, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const before = await readOpsFlags();
    const res = await setOpsFlag(parsed.data.flag, parsed.data.value);
    if (!res.ok) return err(res.error || 'ops_write_failed', 500);

    await logAudit({
      actor: email,
      action: `bot_ops_${parsed.data.value ? 'set' : 'clear'}_${parsed.data.flag}`,
      objectType: 'app_settings',
      objectId: 'dashboard:ops',
      before,
      after: res.next,
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });
    return ok({
      ops: res.next,
      persistence: 'app_settings:dashboard:ops',
      hotPath: 'pending_render_executor',
      note: 'يُطبَّق على العملية الحارة عند إقلاع/إعادة تشغيل المحرك — مُوثق في BOT_CONTROL_CONTRACTS §3.1',
    });
  },
);

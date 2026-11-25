import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { setOpsFlag, readOpsFlags } from '@/lib/bot/engine';
import { botOpsInstant } from '@/lib/bot/engine-gateway';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    flag: z.enum(['filtering_paused', 'alerts_paused']),
    value: z.boolean(),
  })
  .strict();

/**
 * POST /api/bot/ops — persistent + instant ops toggle (admin+).
 * v17.2.0: writes dashboard:ops (persistent, survives restarts) AND attempts a
 * real instant apply on the running engine via the service gateway (/service/bot/ops §3.1).
 * If the engine is asleep the persistent write stands and the response says
 * `deferred_engine_asleep` honestly — hydrate_dashboard will apply it at next boot.
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

    // v17.2.0 §3.1: تطبيق فوري على العملية الحارة بعد نجاح التثبيت
    const flag = parsed.data.flag === 'filtering_paused' ? 'filtering' : 'alerts';
    const inst = await botOpsInstant(flag, parsed.data.value);

    await logAudit({
      actor: email,
      action: `bot_ops_${parsed.data.value ? 'set' : 'clear'}_${parsed.data.flag}`,
      objectType: 'app_settings',
      objectId: 'dashboard:ops',
      before,
      after: { ...res.next, hot_path: inst.ok ? 'instant_applied' : 'deferred_engine_asleep' },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });
    return ok({
      ops: res.next,
      persistence: 'app_settings:dashboard:ops',
      hotPath: inst.ok ? 'instant_applied' : 'deferred_engine_asleep',
      instant: {
        applied: inst.ok,
        engineStatus: inst.status,
        engineOps: inst.data?.ops ?? null,
        error: inst.error || null,
      },
      note: inst.ok
        ? 'كُتب العلم ومُطبّق فوراً على العملية الحارة (§3.1 حي)'
        : 'الكتابة المثابرة نجحت — المحرك غير قابل للوصول الآن فسيرُث العلم عند إقلاعه القادم (hydrate_dashboard) بلا خطوة يدوية',
    });
  },
);

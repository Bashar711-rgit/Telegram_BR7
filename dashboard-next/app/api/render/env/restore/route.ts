import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { upsertEnvVar, triggerDeploy, PROTECTED_ENV_RE } from '@/lib/render';
import { mirrorGetPlain } from '@/lib/bot/env-mirror';
import { logAudit } from '@/lib/audit';
import { z } from 'zod';

export const dynamic = 'force-dynamic';

const Body = z.object({ key: z.string().min(1).max(100).regex(/^[A-Za-z0-9_.]+$/) }).strict();

/**
 * POST /api/render/env/restore — admin+ ; استعادة متغير من مرآة Supabase إلى Render.
 * للحالات: مفتاح موجود في المرآة وحُذف/انحرف على Render (drift)، أو ترميم خاطئ.
 * القيمة تُقرأ مفكوكة خادمياً فقط ولا تعود للعميل أبداً.
 */
export const POST = withApi(
  { name: 'render_env_restore', minRole: 'admin', idempotent: true, rate: { limit: 8, windowMs: 60000 } },
  async ({ email, body, rid }) => {
    const parsed = Body.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { key } = parsed.data;
    if (PROTECTED_ENV_RE.test(key)) return err('protected_env_key', 403);

    const g = await mirrorGetPlain(key);
    if (!g.ok) return err(`restore_failed: ${g.error || 'mirror_missing'}`, 404);

    const r = await upsertEnvVar(key, g.value || '');
    if (r.status !== 200 && r.status !== 201) return err(r.error || `render_http_${r.status}`, 500);

    await logAudit({
      actor: email, action: 'render_env_restore', objectType: 'render_env', objectId: key,
      before: 'from_supabase_mirror', after: { state: 'restored_to_render' },
      requestId: rid,
    });
    const dep = await triggerDeploy();
    if (dep.status === 200 || dep.status === 201) {
      await logAudit({ actor: email, action: 'render_deploy_triggered', objectType: 'render_deploy', objectId: dep.data?.id, after: { reason: 'env_restore', env: key }, requestId: rid });
      return ok({ envKey: key, restored: true, deployTriggered: true, deployId: dep.data?.id });
    }
    return ok({ envKey: key, restored: true, deployTriggered: false });
  },
);

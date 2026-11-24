import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { listEnvVars, upsertEnvVar, triggerDeploy, PROTECTED_ENV_RE } from '@/lib/render';
import { logAudit } from '@/lib/audit';
import { z } from 'zod';

export const dynamic = 'force-dynamic';

/** GET — masked values only. */
export const GET = withApi({ name: 'render_env_get', minRole: 'viewer' }, async () => {
  const r = await listEnvVars();
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  const vars = (r.data || []).map((v) => {
    const e = v?.envVar || { key: '?', value: '' };
    return { key: e.key, value: e.value && e.value.length > 0 ? `•••••(len:${e.value.length})` : '(empty)' };
  });
  return ok({ configured: true, reachable: true, vars });
});

const Body = z.object({ key: z.string().min(1).max(100).regex(/^[A-Za-z0-9_.]+$/), value: z.string().min(1).max(8000) }).strict();

/** POST — admin+ ; protected keys rejected; auto-deploy after change (applies on next deploy). */
export const POST = withApi(
  { name: 'render_env_set', minRole: 'admin', rate: { limit: 10, windowMs: 60000 } },
  async ({ email, body, rid }) => {
    const parsed = Body.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { key, value } = parsed.data;
    if (PROTECTED_ENV_RE.test(key)) return err('protected_env_key', 403);

    const existed = await listEnvVars();
    const hadBefore = existed.status === 200 && (existed.data || []).some((v) => v?.envVar?.key === key);

    const r = await upsertEnvVar(key, value);
    if (r.status !== 200 && r.status !== 201) return err(r.error || `render_http_${r.status}`, 500);
    await logAudit({
      actor: email, action: 'render_env_upsert', objectType: 'render_env', objectId: key,
      before: hadBefore ? 'present' : 'absent', after: 'set',
      requestId: rid,
    });
    const dep = await triggerDeploy();
    if (dep.status === 200 || dep.status === 201) {
      await logAudit({ actor: email, action: 'render_deploy_triggered', objectType: 'render_deploy', objectId: dep.data?.id, after: { reason: 'env_change', env: key }, requestId: rid });
      return ok({ envKey: key, deployTriggered: true, deployId: dep.data?.id });
    }
    return ok({ envKey: key, deployTriggered: false });
  },
);

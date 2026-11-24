import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { deleteEnvVar, PROTECTED_ENV_RE } from '@/lib/render';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** DELETE /api/render/env/:key — admin+ ; protected keys (DATABASE_URL, SECRET_KEY, SESSION_*) rejected. */
export const DELETE = withApi(
  { name: 'render_env_delete', minRole: 'admin', rate: { limit: 10, windowMs: 60000 } },
  async ({ email, seg, rid }) => {
    const { key } = await seg.params;
    if (!key || !/^[A-Za-z0-9_.]+$/.test(key)) return err('invalid_key', 400);
    if (PROTECTED_ENV_RE.test(key)) return err('protected_env_key', 403);
    const r = await deleteEnvVar(key);
    if (r.status !== 204 && r.status !== 200 && r.status !== 201) return err(r.error || `render_http_${r.status}`, 500);
    await logAudit({
      actor: email, action: 'render_env_delete', objectType: 'render_env', objectId: key,
      before: 'present', after: 'deleted', requestId: rid,
    });
    return ok({ envKey: key, deleted: true });
  },
);

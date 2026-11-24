import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { triggerDeploy } from '@/lib/render';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** POST — operator+. Triggers a deploy (cache preserved). */
export const POST = withApi(
  { name: 'render_deploy', minRole: 'operator', rate: { limit: 5, windowMs: 300000 } },
  async ({ email, body, rid }) => {
    const clearCache =
      typeof body === 'object' && body !== null && (body as Record<string, unknown>).clearCache === 'clear' ? 'clear' : 'do_not_clear';
    const r = await triggerDeploy(clearCache);
    if (r.status === 200 || r.status === 201) {
      await logAudit({
        actor: email, action: 'render_deploy_triggered', objectType: 'render_deploy',
        objectId: r.data?.id, after: { clearCache }, requestId: rid,
      });
      return ok({ deployId: r.data?.id, clearCache });
    }
    return err(r.error || `render_http_${r.status}`, 500);
  },
);

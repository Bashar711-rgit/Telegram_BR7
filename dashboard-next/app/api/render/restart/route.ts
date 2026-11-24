import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { restartService } from '@/lib/render';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** POST — operator+. Restart via dedicated endpoint, falling back to deploy. */
export const POST = withApi(
  { name: 'render_restart', minRole: 'operator', rate: { limit: 5, windowMs: 300000 } },
  async ({ email, rid }) => {
    const r = await restartService();
    if (r.status === 200 || r.status === 201) {
      await logAudit({
        actor: email, action: 'render_restart', objectType: 'render_service',
        after: { via: r.via || 'unknown' }, requestId: rid,
      });
      return ok({ via: r.via || 'unknown' });
    }
    return err(r.error || `render_http_${r.status}`, 500);
  },
);

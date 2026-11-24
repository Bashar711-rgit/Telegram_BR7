import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { getService } from '@/lib/render';

export const dynamic = 'force-dynamic';

export const GET = withApi({ name: 'render_service', minRole: 'viewer' }, async () => {
  const r = await getService();
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status, error: r.error });
  const d = r.data as Record<string, unknown>;
  const details = (d.serviceDetails || {}) as Record<string, unknown>;
  return ok({
    configured: true,
    reachable: true,
    id: d.id,
    name: d.name,
    suspended: d.suspended,
    url: details.url,
    updatedAt: d.updatedAt,
  });
});

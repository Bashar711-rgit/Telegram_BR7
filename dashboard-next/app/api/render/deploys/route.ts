import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { listDeploys } from '@/lib/render';

export const dynamic = 'force-dynamic';

export const GET = withApi({ name: 'render_deploys', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } }, async ({ req }) => {
  const limit = Math.min(Math.max(parseInt(req.nextUrl.searchParams.get('limit') || '10', 10) || 10, 1), 20);
  const r = await listDeploys(limit);
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  const deploys = (r.data || []).map((d) => {
    const inner = (d as Record<string, unknown>).deploy || (d as Record<string, unknown>);
    const dep = inner as Record<string, unknown>;
    return {
      id: dep.id,
      status: dep.status,
      createdAt: dep.createdAt,
      finishedAt: dep.finishedAt,
      commitId: dep.commitId,
      commitMessage: dep.commitMessage,
    };
  });
  return ok({ configured: true, reachable: true, deploys });
});

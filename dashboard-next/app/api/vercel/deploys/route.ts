import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { listDeployments } from '@/lib/vercel';

export const dynamic = 'force-dynamic';

export const GET = withApi({ name: 'vercel_deploys', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } }, async () => {
  const r = await listDeployments(8);
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  const deploys = (r.data || []).map((d) => {
    const dep = d as Record<string, unknown>;
    const meta = (dep.meta || {}) as Record<string, unknown>;
    return {
      uid: dep.uid,
      url: dep.url,
      state: dep.readyState || dep.state,
      target: dep.target || (meta.githubCommitRef === 'main' ? 'production' : 'preview'),
      branch: meta.githubCommitRef,
      sha: typeof meta.githubCommitSha === 'string' ? meta.githubCommitSha.slice(0, 8) : null,
      createdAt: dep.createdAt,
    };
  });
  return ok({ configured: true, reachable: true, deploys });
});

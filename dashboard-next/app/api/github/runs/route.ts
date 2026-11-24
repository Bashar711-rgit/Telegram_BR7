import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { listRuns } from '@/lib/github';

export const dynamic = 'force-dynamic';

export const GET = withApi({ name: 'github_runs', minRole: 'viewer' }, async ({ req }) => {
  const limit = Math.min(Math.max(parseInt(req.nextUrl.searchParams.get('limit') || '10', 10) || 10, 1), 30);
  const r = await listRuns(limit);
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  const d = r.data as Record<string, unknown>;
  const runs = ((d.workflow_runs || []) as Array<Record<string, unknown>>).map((x) => ({
    id: x.id,
    name: x.name,
    event: x.event,
    status: x.status,
    conclusion: x.conclusion,
    headBranch: x.head_branch,
    headSha: typeof x.head_sha === 'string' ? x.head_sha.slice(0, 8) : null,
    createdAt: x.created_at,
    htmlUrl: x.html_url,
  }));
  return ok({ configured: true, reachable: true, total: d.total_count, runs });
});

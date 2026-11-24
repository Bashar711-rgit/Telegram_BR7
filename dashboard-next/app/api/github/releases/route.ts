import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { listReleases } from '@/lib/github';

export const dynamic = 'force-dynamic';

export const GET = withApi({ name: 'github_releases', minRole: 'viewer' }, async () => {
  const r = await listReleases(10);
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  const releases = (r.data || []).map((x) => {
    const rel = x as Record<string, unknown>;
    return {
      tagName: rel.tag_name,
      name: rel.name,
      draft: rel.draft,
      prerelease: rel.prerelease,
      publishedAt: rel.published_at,
      htmlUrl: rel.html_url,
    };
  });
  return ok({ configured: true, reachable: true, releases });
});

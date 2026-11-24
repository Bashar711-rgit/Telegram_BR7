import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { listCommits } from '@/lib/github';

export const dynamic = 'force-dynamic';

export const GET = withApi({ name: 'github_commits', minRole: 'viewer' }, async () => {
  const r = await listCommits(10);
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  const commits = (r.data || []).map((x) => {
    const c = x as Record<string, unknown>;
    const commit = (c.commit || {}) as Record<string, unknown>;
    const author = (commit.author || {}) as Record<string, unknown>;
    return {
      sha: typeof c.sha === 'string' ? c.sha.slice(0, 8) : null,
      message: typeof commit.message === 'string' ? commit.message.split('\n')[0].slice(0, 120) : null,
      author: author.name,
      date: author.date,
      htmlUrl: c.html_url,
    };
  });
  return ok({ configured: true, reachable: true, commits });
});

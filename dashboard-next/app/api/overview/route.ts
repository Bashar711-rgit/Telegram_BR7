import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { getService, listDeploys, botHealth } from '@/lib/render';
import { listDeployments } from '@/lib/vercel';
import { repoInfo, listRuns } from '@/lib/github';
import { requireService } from '@/lib/supabase';
import { unstable_cache } from 'next/cache';

export const dynamic = 'force-dynamic';

async function fetchAll() {
  const ts = new Date().toISOString();

  const renderSvc = await getService();
  const renderDeps = await listDeploys(3);
  const render = {
    configured: renderSvc.status !== 0,
    reachable: renderSvc.status === 200,
    name: ((renderSvc.data || {}) as Record<string, unknown>).name || null,
    suspended: ((renderSvc.data || {}) as Record<string, unknown>).suspended ?? null,
    latestDeploy: ((renderDeps.data || [])[0] || null) as Record<string, unknown> | null,
  };

  const vercelDeps = await listDeployments(5);
  const vercel = {
    configured: vercelDeps.status !== 0,
    reachable: vercelDeps.status === 200,
    deploys: (vercelDeps.data || []).slice(0, 4),
  };

  let supabase: Record<string, unknown> = { configured: false, reachable: false };
  try {
    const t0 = Date.now();
    const sb = requireService();
    const { error } = await sb.from('app_settings').select('key').limit(1);
    supabase = { configured: true, reachable: !error, latencyMs: Date.now() - t0 };
  } catch {
    supabase = { configured: false, reachable: false };
  }

  const repo = await repoInfo();
  const runs = await listRuns(1);
  const runData = (runs.data || {}) as Record<string, unknown>;
  const latestRun = ((runData.workflow_runs || []) as Array<Record<string, unknown>>)[0] || null;
  const github = {
    configured: repo.status !== 0,
    reachable: repo.status === 200,
    defaultBranch: ((repo.data || {}) as Record<string, unknown>).default_branch || null,
    pushedAt: ((repo.data || {}) as Record<string, unknown>).pushed_at || null,
    latestRun: latestRun ? { status: latestRun.status, conclusion: latestRun.conclusion, name: latestRun.name } : null,
  };

  const bot = await botHealth();

  return { ts, render, vercel, supabase, github, bot };
}

const getCachedOverview = unstable_cache(fetchAll, ['overview-v1'], { revalidate: 20, tags: ['overview'] });

export const GET = withApi({ name: 'overview', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } }, async () => {
  return ok(await getCachedOverview());
});

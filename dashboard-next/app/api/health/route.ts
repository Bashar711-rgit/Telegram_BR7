import { NextRequest, NextResponse } from 'next/server';
import { rateLimit } from '@/lib/rate-limit';
import { getService, listDeploys, botHealth } from '@/lib/render';
import { listDeployments } from '@/lib/vercel';
import { repoInfo } from '@/lib/github';
import { requireService } from '@/lib/supabase';
import { logger } from '@/lib/logger';

export const dynamic = 'force-dynamic';

/** Public health of the control plane + the 4 services (no secrets in output). */
export async function GET(req: NextRequest) {
  const rid = crypto.randomUUID();
  const ip = (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim();
  if (!rateLimit(`health:${ip}`, 30, 60000)) {
    return NextResponse.json({ ok: false, error: 'rate_limited' }, { status: 429 });
  }
  try {
    const [renderSvc, renderDeps, vercelDeps, repo, bot] = await Promise.all([
      getService(),
      listDeploys(1),
      listDeployments(1),
      repoInfo(),
      botHealth(),
    ]);
    let supabase = { configured: false, reachable: false };
    try {
      const sb = requireService();
      const { error } = await sb.from('app_settings').select('key').limit(1);
      supabase = { configured: true, reachable: !error };
    } catch {
      /* not configured */
    }
    const body = {
      ok: true,
      data: {
        panel: 'ok',
        ts: new Date().toISOString(),
        services: {
          render: { configured: renderSvc.status !== 0, reachable: renderSvc.status === 200, latestDeployStatus: ((renderDeps.data || [])[0] as Record<string, unknown> | undefined)?.status ?? null },
          vercel: { configured: vercelDeps.status !== 0, reachable: vercelDeps.status === 200 },
          supabase,
          github: { configured: repo.status !== 0, reachable: repo.status === 200 },
        },
        bot_health: { reachable: bot.reachable, status: bot.status ?? null, db_healthy: bot.dbHealthy ?? null },
      },
    };
    return NextResponse.json(body, { headers: { 'x-request-id': rid, 'Cache-Control': 'no-store' } });
  } catch (e) {
    logger.error({ err: String(e), rid }, 'health_error');
    return NextResponse.json({ ok: false, error: 'internal_error', code: rid }, { status: 500 });
  }
}

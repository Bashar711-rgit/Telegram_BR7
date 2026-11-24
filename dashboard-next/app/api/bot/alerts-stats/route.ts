import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { requireService } from '@/lib/supabase';

export const dynamic = 'force-dynamic';

type AlertRow = {
  alert_status: string | null;
  notification_attempts: number | null;
  notification_error: string | null;
  decision: string | null;
  confidence: number | null;
  alert_sent_at: string | null;
  created_at: string;
};

/** GET /api/bot/alerts-stats — REAL notification delivery stats from actual alert rows. */
export const GET = withApi(
  { name: 'bot_alerts_stats', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } },
  async () => {
    const sb = requireService();
    const N = 1000;
    const { data, error } = await sb
      .from('alerts')
      .select('alert_status,notification_attempts,notification_error,decision,confidence,alert_sent_at,created_at')
      .order('id', { ascending: false })
      .limit(N);
    if (error) return err(error.message, 500);
    const rows = (data ?? []) as unknown as AlertRow[];
    const total = rows.length;

    const byStatus: Record<string, number> = {};
    let attemptsSum = 0;
    let withError = 0;
    let sent = 0;
    const errors = new Map<string, number>();
    let confSum = 0;
    let confCount = 0;

    for (const r of rows) {
      const s = (r.alert_status || 'unknown').toLowerCase();
      byStatus[s] = (byStatus[s] || 0) + 1;
      if (s === 'sent' || r.alert_sent_at) sent++;
      if (typeof r.notification_attempts === 'number') attemptsSum += r.notification_attempts;
      if (r.notification_error) {
        withError++;
        const k = String(r.notification_error).slice(0, 80);
        errors.set(k, (errors.get(k) || 0) + 1);
      }
      if (typeof r.confidence === 'number') {
        confSum += r.confidence;
        confCount++;
      }
    }

    return ok({
      sample: { size: total, cap: N },
      byStatus,
      sent,
      successRate: total ? Number((sent / total).toFixed(4)) : null,
      totalAttempts: attemptsSum,
      attemptsPerAlert: total ? Number((attemptsSum / total).toFixed(3)) : null,
      withErrorCount: withError,
      topErrors: [...errors.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([errorText, count]) => ({ error: errorText, count })),
      avgConfidence: confCount ? Number((confSum / confCount).toFixed(4)) : null,
    });
  },
);

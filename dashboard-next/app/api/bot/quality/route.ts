import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { requireService } from '@/lib/supabase';
import { registryStats, getRegistry } from '@/lib/bot/keywords';

export const dynamic = 'force-dynamic';

type Decision = {
  decision: string | null;
  confidence: number | null;
  urgent: boolean | null;
  msg_hash: string | null;
  reasons: string | null;
  created_at: string;
};

/**
 * GET /api/bot/quality — REAL quality metrics computed from actual filter_decisions
 * rows (last N). Precision/Recall/F1 are reported as unavailable unless reference
 * labels exist — never invented.
 */
export const GET = withApi(
  { name: 'bot_quality', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } },
  async () => {
    const sb = requireService();
    const N = 2000;
    const { data, error } = await sb
      .from('filter_decisions')
      .select('decision,confidence,urgent,msg_hash,reasons,created_at')
      .order('id', { ascending: false })
      .limit(N);
    if (error) return err(error.message, 500);
    const rows = (data ?? []) as unknown as Decision[];
    const total = rows.length;
    const byDecision: Record<string, number> = {};
    let confSum = 0;
    let confCount = 0;
    let urgent = 0;
    const reasons = new Map<string, number>();
    const hashSeen = new Set<string>();
    let hashDup = 0;
    let labeled = 0;

    for (const r of rows) {
      const d = (r.decision || 'unknown').toLowerCase();
      byDecision[d] = (byDecision[d] || 0) + 1;
      if (typeof r.confidence === 'number') {
        confSum += r.confidence;
        confCount++;
      }
      if (r.urgent) urgent++;
      if (r.msg_hash) {
        if (hashSeen.has(r.msg_hash)) hashDup++;
        hashSeen.add(r.msg_hash);
      }
      if (r.reasons) {
        try {
          const arr = JSON.parse(r.reasons) as unknown;
          if (Array.isArray(arr)) {
            for (const x of arr.slice(0, 4)) {
              const k = String(x).slice(0, 60);
              reasons.set(k, (reasons.get(k) || 0) + 1);
            }
          }
        } catch {
          const k = String(r.reasons).slice(0, 60);
          reasons.set(k, (reasons.get(k) || 0) + 1);
        }
      }
    }

    // Reference labels: filter_decisions.feedback exists in schema but is NOT in the
    // granted select columns for this phase — precision/recall stay honest/unavailable.
    const reg = await getRegistry();
    const registry = reg.ok ? registryStats(reg.registry) : null;

    return ok({
      sample: { size: total, window: 'آخر الصفوف', cap: N },
      byDecision,
      avgConfidence: confCount ? Number((confSum / confCount).toFixed(4)) : null,
      confidenceCount: confCount,
      urgentRate: total ? Number((urgent / total).toFixed(4)) : null,
      hashDuplicateRate: total ? Number((hashDup / total).toFixed(4)) : null,
      topReasons: [...reasons.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([reason, count]) => ({ reason, count })),
      metrics: {
        precision: null,
        recall: null,
        f1: null,
        note: 'غير متاح — لا توجد بيانات تصنيف مرجعية ممنوحة للقراءة في هذه المرحلة (عقد §3.6). لا تُحسب دون مرجعية.',
      },
      keywordRegistry: registry ? { totalWords: registry.totalWords, groups: registry.groups.length, duplicates: registry.duplicates.length } : null,
      labeledCount: labeled,
    });
  },
);

import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { engineAnalyze } from '@/lib/bot/engine-gateway';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({ text: z.string().min(1).max(2000) })
  .strict();

/**
 * POST /api/bot/analyze — v17.2.0 §3.5 حي: مقعد اختبار التحليل.
 * يمرر النص إلى الفلتر الحي + المصنّف + البوابة السريعة على المحرك ويعيد
 * الحكم الحقيقي (قرار/ثقة/أسباب/تصنيف) — حسابية قراءة بحتة بلا أثر جانبي.
 */
export const POST = withApi(
  { name: 'bot_analyze', minRole: 'viewer', mutating: true, rate: { limit: 20, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const r = await engineAnalyze(parsed.data.text);
    const d = r.data;

    await logAudit({
      actor: email,
      action: 'engine_analyze_test_bench',
      objectType: 'engine',
      objectId: 'analyze',
      before: null,
      after: {
        text_len: parsed.data.text.length,
        decision: d?.verdict?.decision ?? null,
        confidence: d?.verdict?.confidence ?? null,
        latency_ms: d?.latency_ms ?? null,
        engineStatus: r.status,
      },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });

    if (!r.ok) {
      return err(
        r.status === 0 ? `engine_unreachable: ${r.error || 'لا استجابة'}` : `engine_error_${r.status}: ${String(d?.error || r.error || '')}`,
        r.status === 0 ? 504 : 502,
      );
    }
    return ok({
      verdict: d?.verdict ?? null,
      classification: d?.classification ?? null,
      fastGate: d?.fast_gate ?? null,
      latencyMs: d?.latency_ms ?? null,
      note: 'تحليل حقيقي عبر EnhancedFilter + classifier + FastGate على العملية الحارة (§3.5) — بلا أي أثر جانبي',
    });
  },
);

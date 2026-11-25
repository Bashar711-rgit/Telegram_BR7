import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { sourcesCheck, type SourceCheckResult } from '@/lib/bot/engine-gateway';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    chat_id: z.string().max(40).optional(),
    username: z.string().max(80).optional(),
  })
  .strict()
  .refine((v) => (v.chat_id || v.username || '').trim().length > 0, { message: 'missing_target' });

/**
 * POST /api/bot/sources/check — v17.2.0 §3.4 حي.
 * فحص وصول حقيقي لمصدر عبر عميل تيليجرام الحي على المحرك:
 * الوصول للكيان + حالة العضوية + صلاحية الإرسال. قراءة-فحص بلا أثر جانبي.
 * يفشل بصدق إن كان المحرك نائماً أو لا يوجد عميل متصل.
 */
export const POST = withApi(
  { name: 'bot_sources_check', minRole: 'operator', mutating: true, rate: { limit: 10, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'missing_target'}`, 400);

    const r = await sourcesCheck({
      chatId: (parsed.data.chat_id || '').trim(),
      username: (parsed.data.username || '').trim(),
    });
    const d = r.data as SourceCheckResult | null;
    const failed = !r.ok;

    await logAudit({
      actor: email,
      action: 'source_access_check',
      objectType: 'sources',
      objectId: String(parsed.data.chat_id || parsed.data.username || '').slice(0, 64),
      before: null,
      after: failed
        ? { engineStatus: r.status, error: r.error || null }
        : {
            reachable: d?.reachable ?? null,
            member_status: d?.member_status ?? null,
            entity: d?.entity ? { id: d.entity.id, title: d.entity.title, username: d.entity.username } : null,
          },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });

    if (failed) {
      return err(
        r.status === 0 ? `engine_unreachable: ${r.error || 'لا استجابة'}` : `engine_error_${r.status}: ${d?.error || r.error || ''}`,
        r.status === 0 ? 504 : 502,
      );
    }
    return ok({
      reachable: d?.reachable ?? false,
      entity: d?.entity ?? null,
      memberStatus: d?.member_status ?? null,
      canSend: d?.can_send ?? null,
      detail: d?.error || d?.note || null,
      note: d?.reachable
        ? 'فحص حي عبر عميل تيليجرام المتصل فعلاً على المحرك (§3.4)'
        : 'فحص حي — تعذر الوصول للكيان من الحساب المراقب (نتيجة حقيقية لا افتراض)',
    });
  },
);

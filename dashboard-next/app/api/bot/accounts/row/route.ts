import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { rowDelete, rowToggle } from '@/lib/bot/accounts-flow';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    row_id: z.number().int().positive(),
    action: z.enum(['delete', 'toggle']),
    enable: z.boolean().optional(),
  })
  .strict();

type EngineRes = { ok?: boolean; enable?: boolean; error?: string };

/**
 * POST /api/bot/accounts/row — إدارة صف حساب قائم عبر المحرك (hot).
 * - toggle (admin+): تفعيل/إيقاف المراقبة فوراً على الحلقة الرئيسية (spawn/stop)
 *   مع تحديث panel_accounts — لا انتظار لإعادة التشغيل.
 * - delete (super_admin فقط): حذف الصف + إيقاف المراقب — محمي بتأكيد في الواجهة.
 */
export const POST = withApi(
  { name: 'bot_accounts_row', minRole: 'admin', mutating: true, rate: { limit: 12, windowMs: 60000 } },
  async ({ body, email, role, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { row_id, action } = parsed.data;

    if (action === 'delete' && role !== 'super_admin') {
      return err('forbidden: حذف حساب يتطلب دور super_admin', 403);
    }
    if (action === 'toggle' && typeof parsed.data.enable !== 'boolean') {
      return err('invalid_body: enable مطلوب لعملية toggle', 400);
    }

    const res =
      action === 'delete' ? await rowDelete(row_id) : await rowToggle(row_id, Boolean(parsed.data.enable));

    const d = (res.data || {}) as EngineRes;
    const outcome = res.status === 0 || res.error ? `unreachable:${res.error || 'timeout'}` : d.ok ? 'ok' : d.error || `engine_${res.status}`;

    await logAudit({
      actor: email,
      action: `bot_accounts_row_${action}`,
      objectType: 'panel_accounts',
      objectId: String(row_id),
      after: { outcome, ...(action === 'toggle' ? { enable: parsed.data.enable } : {}) },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });

    if (res.status === 0 || res.error) return err(`engine_unreachable: ${res.error || 'timeout'}`, 504);
    if (!res.data) return err(`engine_error_${res.status}`, res.status >= 400 ? res.status : 502);
    if (d.ok) return ok({ enable: d.enable ?? null });
    return err(d.error || `engine_error_${res.status}`, 400);
  },
);

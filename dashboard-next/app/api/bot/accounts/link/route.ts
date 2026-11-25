import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { linkStart, linkCode, linkPassword, linkCancel, linkStatus } from '@/lib/bot/accounts-flow';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const opId = z.string().regex(/^[0-9a-f]{8,64}$/i, 'op_id غير صالح');

const schema = z.discriminatedUnion('action', [
  z.object({ action: z.literal('start'), phone: z.string().min(8).max(24) }).strict(),
  z
    .object({ action: z.literal('code'), op_id: opId, code: z.string().regex(/^\d{4,10}$/) })
    .strict(),
  z
    .object({ action: z.literal('password'), op_id: opId, password: z.string().min(1).max(128) })
    .strict(),
  z.object({ action: z.literal('cancel'), op_id: opId }).strict(),
]);

type EngineRes = {
  ok?: boolean;
  op?: { state?: string; error_code?: string | null; error_message?: string | null; flood_wait?: number } | null;
  cancelled?: boolean;
  error_code?: string;
  error_message?: string;
  flood_wait?: number;
  error?: string;
  note?: string;
};

function maskPhone(p: string): string {
  return p.length >= 5 ? `${p.slice(0, 3)}***${p.slice(-2)}` : '***';
}

/**
 * POST /api/bot/accounts/link — معالج الإضافة الحقيقي (admin+).
 * وسيط صارم بين اللوحة وبوابة /service/accounts على المحرك: Zod strict،
 * ثم تحويل 1:1 إلى link_manager لدى المحرك. OTP وكلمة 2FA عابران ولا
 * يُسجَّلان في audit أبداً — يُسجَّل فقط نوع الخطوة + الناتج.
 */
export const POST = withApi(
  { name: 'bot_accounts_link', minRole: 'admin', mutating: true, rate: { limit: 12, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const cmd = parsed.data;

    const auditOutcome = async (objectId: string, outcome: string) => {
      await logAudit({
        actor: email,
        action: `bot_accounts_link_${cmd.action}`,
        objectType: 'engine_link_flow',
        objectId,
        after: { outcome },
        ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
        userAgent: req.headers.get('user-agent') || '',
        requestId: rid,
      });
    };

    let res;
    if (cmd.action === 'start') res = await linkStart(cmd.phone);
    else if (cmd.action === 'code') res = await linkCode(cmd.op_id, cmd.code);
    else if (cmd.action === 'password') res = await linkPassword(cmd.op_id, cmd.password);
    else res = await linkCancel(cmd.op_id);

    if (res.status === 0 || res.error) {
      await auditOutcome(
        cmd.action === 'start' ? maskPhone(cmd.phone) : 'op_id' in cmd ? cmd.op_id.slice(0, 8) : '',
        `unreachable:${res.error || 'timeout'}`,
      );
      return err(`engine_unreachable: ${res.error || 'timeout'}`, 504);
    }
    const d = (res.data || {}) as EngineRes;
    const state = d.op?.state || (d.cancelled ? 'cancelled' : '');
    const outcome = d.ok ? state || 'ok' : d.error_code || d.error || `engine_${res.status}`;

    // التدقيق: لا رمز ولا كلمة مرور ولا session — الناتج فقط
    if (cmd.action === 'start') await auditOutcome(maskPhone(cmd.phone), outcome);
    else if ('op_id' in cmd) await auditOutcome(cmd.op_id.slice(0, 8), outcome);

    if (!res.data) return err(`engine_error_${res.status}`, res.status >= 400 ? res.status : 502);
    if (d.ok) return ok({ op: d.op ?? null, cancelled: d.cancelled ?? null });

    const floodNote = d.flood_wait ? ` — انتظر ${d.flood_wait} ثانية` : '';
    return err(
      `${d.error_code || 'engine_error'}: ${d.error_message || d.error || 'فشل غير محدد'}${floodNote}`,
      400,
    );
  },
);

/** GET /api/bot/accounts/link?op_id=… — استعلام حالة العملية (TTL 15 دقيقة). */
export const GET = withApi(
  { name: 'bot_accounts_link_status', minRole: 'admin', mutating: false, rate: { limit: 30, windowMs: 60000 } },
  async ({ req, email, rid }) => {
    const opId = req.nextUrl.searchParams.get('op_id') || '';
    if (!opId || !/^[0-9a-f]{8,64}$/i.test(opId)) return err('invalid_op_id', 400);
    const res = await linkStatus(opId);
    if (res.status === 0 || res.error) return err(`engine_unreachable: ${res.error || 'timeout'}`, 504);
    const d = (res.data || {}) as EngineRes & { op?: unknown };
    if (res.status === 404) return err('op_not_found: لا توجد عملية بهذا المعرّف (انتهت أو أُلغيت)', 404);
    if (d.ok) {
      await logAudit({
        actor: email,
        action: 'bot_accounts_link_status',
        objectType: 'engine_link_flow',
        objectId: opId.slice(0, 8),
        after: { outcome: d.op && typeof d.op === 'object' ? String((d.op as { state?: string }).state) : 'ok' },
        ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
        userAgent: req.headers.get('user-agent') || '',
        requestId: rid,
      });
      return ok({ op: d.op ?? null });
    }
    return err(`${d.error_code || 'engine_error'}: ${d.error_message || d.error || 'فشل غير محدد'}`, 400);
  },
);

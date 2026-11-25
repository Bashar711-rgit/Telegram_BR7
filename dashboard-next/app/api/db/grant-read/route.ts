import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { dbGrantRead } from '@/lib/bot/engine-gateway';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const ALLOWED = ['sender_stats', 'blocked_chats', 'notifications'] as const;

const schema = z
  .object({ tables: z.array(z.enum(ALLOWED)).min(1).max(3) })
  .strict();

/**
 * POST /api/db/grant-read — v17.2.0: منح قراءة للوحة على الجداول الثلاثة
 * التي كان منحها يتطلب SQL يدوي في Supabase SQL Editor.
 * المسار حقيقي من طرف إلى طرف: اللوحة → بوابة الخدمة (سر مشترك) → قاعدة
 * Supabase عبر اتصال المحرك — بقائمة بيضاء صارمة بلا SQL حر.
 * يفشل بصدق إن رفض دور قاعدة المحرك عملية GRANT.
 */
export const POST = withApi(
  { name: 'db_grant_read', minRole: 'super_admin', mutating: true, rate: { limit: 5, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const r = await dbGrantRead(parsed.data.tables);
    const d = r.data;

    await logAudit({
      actor: email,
      action: 'db_grant_read',
      objectType: 'postgres',
      objectId: parsed.data.tables.join(','),
      before: null,
      after: r.ok ? d?.results ?? null : { engineStatus: r.status, error: r.error || null },
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
    const results = d?.results ?? [];
    const allGranted = results.every((x) => x.granted);
    return ok({
      results,
      allGranted,
      note: allGranted
        ? 'نُفّذ GRANT SELECT فعلياً عبر قاعدة Supabase — الصفحات الثلاثة تعمل الآن'
        : 'بعض المنح فشل بصدق — تفاصيل كل جدول في results (لا نجاح جزئي وهمي)',
    });
  },
);

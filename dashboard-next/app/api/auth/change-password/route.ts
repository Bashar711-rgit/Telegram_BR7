import { z } from 'zod';
import { withApi, sameOrigin } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY } from '@/lib/supabase';
import { logAudit } from '@/lib/audit';
import { fetchJson } from '@/lib/http';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    currentPassword: z.string().min(8).max(128),
    newPassword: z.string().min(8).max(128),
  })
  .strict();

/**
 * POST /api/auth/change-password — self-service, real:
 * 1) verify current password via Supabase Auth (anon, server-side)
 * 2) update the auth user password via service_role admin API
 * 3) audit with masked values (passwords never logged)
 */
export const POST = withApi(
  { name: 'auth_change_password', mutating: true, rate: { limit: 5, windowMs: 15 * 60000 } },
  async ({ req, body, email, rid }) => {
    // Belt & braces: change-password is itself a mutation — require same-origin.
    if (!sameOrigin(req)) return err('csrf_rejected', 403);

    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    if (!SUPABASE_URL || !SUPABASE_ANON_KEY || !SUPABASE_SERVICE_KEY) return err('supabase_not_configured', 500);

    // Single verification call — anon grant_type=password validates the current
    // password AND returns the auth user id needed for the admin update.
    try {
      const r = await fetchJson<{ access_token?: string; user?: { id?: string } }>(
        `${SUPABASE_URL}/auth/v1/token?grant_type=password`,
        {
          method: 'POST',
          headers: { apikey: SUPABASE_ANON_KEY, 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password: parsed.data.currentPassword }),
          timeoutMs: 15000,
        },
      );
      const uid = r.data?.user?.id;
      if (r.status !== 200 || !uid) return err('current_password_invalid', 403);

      const upd = await fetchJson(`${SUPABASE_URL}/auth/v1/admin/users/${uid}`, {
        method: 'PUT',
        headers: { apikey: SUPABASE_SERVICE_KEY, Authorization: `Bearer ${SUPABASE_SERVICE_KEY}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: parsed.data.newPassword }),
        timeoutMs: 15000,
      });
      if (upd.status !== 200) return err(`password_update_failed_${upd.status}`, 500);

      await logAudit({
        actor: email,
        action: 'dashboard_password_changed',
        objectType: 'auth',
        objectId: email,
        before: { password: '•••••(len:hidden)' },
        after: { password: '•••••(len:hidden)' },
        requestId: rid,
      });
      return ok({ changed: true, note: 'كلمة المرور تحدّثت فعلياً — استخدمها في الدخول القادم' });
    } catch (e) {
      return err('password_update_failed', 500);
    }
  },
);

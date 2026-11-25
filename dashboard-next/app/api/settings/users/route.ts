import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { requireService } from '@/lib/supabase';
import { logAudit } from '@/lib/audit';
import { z } from 'zod';
import { ROLES } from '@/lib/auth';

export const dynamic = 'force-dynamic';

const RoleEnum = z.enum(ROLES);

const CreateBody = z
  .object({ email: z.string().email().max(200), password: z.string().min(8).max(128), role: RoleEnum })
  .strict();
const PatchBody = z
  .object({ email: z.string().email().max(200), role: RoleEnum.optional(), enabled: z.boolean().optional() })
  .strict();

/** GET — super_admin: list dashboard users. */
export const GET = withApi({ name: 'users_get', minRole: 'super_admin' }, async () => {
  const sb = requireService();
  const { data, error } = await sb
    .from('dashboard_users')
    .select('id,username,role,enabled,created_at,last_login_at')
    .order('id', { ascending: true });
  if (error) return err(error.message, 500);
  return ok({ users: data ?? [] });
});

/** POST — super_admin: create Supabase Auth user + registry row. */
export const POST = withApi(
  { name: 'users_create', minRole: 'super_admin', idempotent: true, rate: { limit: 10, windowMs: 300000 } },
  async ({ email: actor, body, rid }) => {
    const parsed = CreateBody.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { email, password, role } = parsed.data;
    const sb = requireService();

    const { error: authErr } = await sb.auth.admin.createUser({
      email,
      password,
      email_confirm: true,
      user_metadata: { source: 'br7-control-plane', createdBy: actor },
    });
    if (authErr && !String(authErr.message).includes('already')) return err(authErr.message, 400);

    const { error: rowErr } = await sb
      .from('dashboard_users')
      .upsert(
        { username: email, password_hash: '(managed-by-supabase-auth)', role, enabled: 1 },
        { onConflict: 'username' },
      );
    if (rowErr) return err(rowErr.message, 500);
    await logAudit({
      actor, action: 'dashboard_user_created', objectType: 'dashboard_user', objectId: email,
      after: { role, enabled: 1 }, requestId: rid,
    });
    return ok({ email, role, created: true });
  },
);

/** PATCH — super_admin: change role / enable-disable (revocation takes effect on next API call). */
export const PATCH = withApi(
  { name: 'users_patch', minRole: 'super_admin', idempotent: true, rate: { limit: 20, windowMs: 60000 } },
  async ({ email: actor, body, rid }) => {
    const parsed = PatchBody.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { email, role, enabled } = parsed.data;
    const sb = requireService();

    const { data: before } = await sb.from('dashboard_users').select('role,enabled').eq('username', email).maybeSingle();
    if (!before) return err('not_found', 404);

    const upd: Record<string, unknown> = {};
    if (role) upd.role = role;
    if (enabled !== undefined) upd.enabled = enabled ? 1 : 0;
    if (Object.keys(upd).length === 0) return err('nothing_to_update', 400);

    const { error } = await sb.from('dashboard_users').update(upd).eq('username', email);
    if (error) return err(error.message, 500);
    await logAudit({
      actor, action: 'dashboard_user_updated', objectType: 'dashboard_user', objectId: email,
      before, after: upd, requestId: rid,
    });
    return ok({ email, ...upd });
  },
);

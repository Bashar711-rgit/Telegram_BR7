import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { saveProfile, listProfiles, deleteProfile } from '@/lib/bot/keywords';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** GET /api/bot/profiles — saved analysis profiles (KV persistence is real). */
export const GET = withApi(
  { name: 'bot_profiles_get', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } },
  async () => ok({ profiles: await listProfiles() }),
);

const schema = z
  .object({
    name: z.string().min(2).max(40).regex(/^[a-z0-9_\-]+$/i),
    config: z.record(z.string(), z.unknown()),
  })
  .strict();

/** POST /api/bot/profiles — save a profile (real KV write, audited). Applying to the running engine is contract §3.2. */
export const POST = withApi(
  { name: 'bot_profiles_post', minRole: 'admin', mutating: true, rate: { limit: 20, windowMs: 60000 } },
  async ({ body, email, rid }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const r = await saveProfile(parsed.data.name, parsed.data.config);
    if (!r.ok) return err(r.error || 'save_failed', 400);
    await logAudit({
      actor: email,
      action: 'filter_profile_save',
      objectType: 'app_settings',
      objectId: `dashboard:filter_profile:${parsed.data.name}`,
      before: null,
      after: { name: parsed.data.name, config: parsed.data.config },
      requestId: rid,
    });
    return ok({ name: parsed.data.name, saved: true, engineApplication: 'عقد §3.2 — الحفظ حقيقي والتطبيق الحار ينتظر منفذ Render' });
  },
);

const delSchema = z.object({ name: z.string().min(2).max(40).regex(/^[a-z0-9_\-]+$/i) }).strict();

export const DELETE = withApi(
  { name: 'bot_profiles_delete', minRole: 'admin', mutating: true, rate: { limit: 20, windowMs: 60000 } },
  async ({ body, email, rid }) => {
    const parsed = delSchema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const r = await deleteProfile(parsed.data.name);
    if (!r.ok) return err(r.error || 'delete_failed', 400);
    await logAudit({
      actor: email,
      action: 'filter_profile_delete',
      objectType: 'app_settings',
      objectId: `dashboard:filter_profile:${parsed.data.name}`,
      before: { name: parsed.data.name },
      after: null,
      requestId: rid,
    });
    return ok({ name: parsed.data.name, deleted: true });
  },
);

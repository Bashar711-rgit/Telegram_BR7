import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { listEnvVars, upsertEnvVar, triggerDeploy, PROTECTED_ENV_RE } from '@/lib/render';
import { mirrorSet, mirrorList } from '@/lib/bot/env-mirror';
import { logAudit } from '@/lib/audit';
import { z } from 'zod';

export const dynamic = 'force-dynamic';

/**
 * GET — قيم مموّهة دائماً + حالة المرآة لكل مفتاح.
 * يدمج: متغيرات Render الحية + مفاتيح المرآة في Supabase (للكشف عن drift
 * أو مفاتيح حُذفت من Render وبقيت محفوظة مشفّرة في Supabase).
 */
export const GET = withApi({ name: 'render_env_get', minRole: 'viewer' }, async () => {
  const r = await listEnvVars();
  const mir = await mirrorList();
  if (r.status === 0) return ok({ configured: false, error: r.error, mirror: mir.entries });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status, mirror: mir.entries });

  const mirrorMap = new Map(mir.entries.map((m) => [m.key, m]));
  const vars = (r.data || []).map((v) => {
    const e = v?.envVar || { key: '?', value: '' };
    const m = mirrorMap.get(e.key);
    return {
      key: e.key,
      value: e.value && e.value.length > 0 ? `•••••(len:${e.value.length})` : '(empty)',
      mirrored: Boolean(m),
      mirrorUpdatedAt: m?.updatedAt || null,
      mirrorBroken: Boolean(m?.broken),
    };
  });
  const renderKeys = new Set(vars.map((v) => v.key));
  const mirrorOnly = mir.entries
    .filter((m) => !renderKeys.has(m.key))
    .map((m) => ({ key: m.key, updatedAt: m.updatedAt, broken: Boolean(m.broken) }));
  return ok({ configured: true, reachable: true, vars, mirrorOnly });
});

const Body = z.object({ key: z.string().min(1).max(100).regex(/^[A-Za-z0-9_.]+$/), value: z.string().min(1).max(8000) }).strict();

/**
 * POST — admin+ ; حفظ مزدوج: Render (مصدر التشغيل) + مرآة Supabase مشفّرة.
 * المفاتيح المحمية مرفوضة؛ أي تعديل يُشغّل نشراً تلقائياً (تطبيق على المحرك).
 */
export const POST = withApi(
  { name: 'render_env_set', minRole: 'admin', idempotent: true, rate: { limit: 10, windowMs: 60000 } },
  async ({ email, body, rid }) => {
    const parsed = Body.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);
    const { key, value } = parsed.data;
    if (PROTECTED_ENV_RE.test(key)) return err('protected_env_key', 403);

    const existed = await listEnvVars();
    const hadBefore = existed.status === 200 && (existed.data || []).some((v) => v?.envVar?.key === key);

    const r = await upsertEnvVar(key, value);
    if (r.status !== 200 && r.status !== 201) return err(r.error || `render_http_${r.status}`, 500);

    // الحفظ المزدوج: المرآة في Supabase (فشلها لا يُفشل العملية — Render هو التشغيل)
    const mir = await mirrorSet(key, value);

    await logAudit({
      actor: email, action: 'render_env_upsert', objectType: 'render_env', objectId: key,
      before: hadBefore ? 'present' : 'absent',
      after: { state: 'set', dualPersist: mir.ok ? 'render+supabase' : 'render_only', mirrorError: mir.error || null },
      requestId: rid,
    });
    const dep = await triggerDeploy();
    if (dep.status === 200 || dep.status === 201) {
      await logAudit({ actor: email, action: 'render_deploy_triggered', objectType: 'render_deploy', objectId: dep.data?.id, after: { reason: 'env_change', env: key }, requestId: rid });
      return ok({ envKey: key, deployTriggered: true, deployId: dep.data?.id, mirrored: mir.ok, mirrorError: mir.error || null });
    }
    return ok({ envKey: key, deployTriggered: false, mirrored: mir.ok, mirrorError: mir.error || null });
  },
);

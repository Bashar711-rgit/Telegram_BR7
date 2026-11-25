import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { deleteEnvVar, PROTECTED_ENV_RE } from '@/lib/render';
import { mirrorDelete } from '@/lib/bot/env-mirror';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/**
 * DELETE /api/render/env/:key — admin+ ; حذف مزدوج: من Render ومن مرآة Supabase.
 * المفاتيح المحمية (DATABASE_URL، SECRET_KEY، SESSION_*) مرفوضة تلقائياً.
 */
export const DELETE = withApi(
  { name: 'render_env_delete', minRole: 'admin', idempotent: true, rate: { limit: 10, windowMs: 60000 } },
  async ({ email, seg, rid }) => {
    const { key } = await seg.params;
    if (!key || !/^[A-Za-z0-9_.]+$/.test(key)) return err('invalid_key', 400);
    if (PROTECTED_ENV_RE.test(key)) return err('protected_env_key', 403);
    const r = await deleteEnvVar(key);
    if (r.status !== 204 && r.status !== 200 && r.status !== 201) return err(r.error || `render_http_${r.status}`, 500);
    // تنظيف المرآة أيضاً — لا نترك قيمة قديمة قابلة للاستعادة بعد حذف متعمد
    const mir = await mirrorDelete(key);
    await logAudit({
      actor: email, action: 'render_env_delete', objectType: 'render_env', objectId: key,
      before: 'present',
      after: { state: 'deleted', mirrorCleared: mir.ok, mirrorError: mir.error || null },
      requestId: rid,
    });
    return ok({ envKey: key, deleted: true, mirrorCleared: mir.ok, mirrorError: mir.error || null });
  },
);

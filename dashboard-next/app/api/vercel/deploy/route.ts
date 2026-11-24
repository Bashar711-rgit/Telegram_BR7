import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { redeployPreview, listDeployments } from '@/lib/vercel';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/**
 * POST — operator+. Rebuilds a PREVIEW of the working branch.
 * Production deploys are intentionally NOT exposed by the control plane.
 */
export const POST = withApi(
  { name: 'vercel_redeploy', minRole: 'operator', rate: { limit: 5, windowMs: 300000 } },
  async ({ email, body, rid }) => {
    let deploymentId: string | undefined;
    if (typeof body === 'object' && body !== null) {
      const v = (body as Record<string, unknown>).deploymentId;
      if (typeof v === 'string' && v.startsWith('dpl_')) deploymentId = v;
    }
    if (!deploymentId) {
      const latest = await listDeployments(5);
      const preview = (latest.data || []).find(
        (d) => (d as Record<string, unknown>).target !== 'production' && (d as Record<string, unknown>).readyState === 'READY',
      );
      deploymentId = preview ? ((preview as Record<string, unknown>).uid as string) : undefined;
    }
    const r = await redeployPreview(deploymentId);
    if (r.status === 200 || r.status === 201) {
      const d = (r.data || {}) as Record<string, unknown>;
      await logAudit({
        actor: email, action: 'vercel_redeploy_preview', objectType: 'vercel_deploy',
        objectId: ((d.id || d.uid) as string | number | null) ?? null, after: { deploymentId: deploymentId || null }, requestId: rid,
      });
      return ok({ uid: d.id || d.uid, url: d.url });
    }
    return err(r.error || `vercel_http_${r.status}`, 500);
  },
);

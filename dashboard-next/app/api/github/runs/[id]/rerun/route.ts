import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { rerunRun } from '@/lib/github';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** POST /api/github/runs/:id/rerun — admin+. */
export const POST = withApi(
  { name: 'github_rerun', minRole: 'admin', rate: { limit: 5, windowMs: 300000 } },
  async ({ email, seg, rid }) => {
    const { id } = await seg.params;
    const runId = parseInt(id, 10);
    if (!Number.isInteger(runId)) return err('invalid_run_id', 400);
    const r = await rerunRun(runId);
    if (r.status === 201) {
      await logAudit({
        actor: email, action: 'github_run_rerun', objectType: 'github_run', objectId: runId,
        requestId: rid,
      });
      return ok({ rerun: true, runId });
    }
    return err(r.error || `github_http_${r.status}`, r.status || 500);
  },
);

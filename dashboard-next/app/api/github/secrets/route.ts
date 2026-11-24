import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { listSecretNames } from '@/lib/github';

export const dynamic = 'force-dynamic';

/** GET — super_admin only. Secret NAMES only; values are never retrievable by API design. */
export const GET = withApi({ name: 'github_secrets', minRole: 'super_admin' }, async () => {
  const r = await listSecretNames();
  if (r.status === 0) return ok({ configured: false, error: r.error });
  if (r.status !== 200 || !r.data) return ok({ configured: true, reachable: false, status: r.status });
  return ok({ configured: true, reachable: true, names: r.data });
});

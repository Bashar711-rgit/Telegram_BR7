import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';

export const dynamic = 'force-dynamic';

/** GET — super_admin. Key NAMES + configured/unconfigured state only. Values never leave the server. */
export const GET = withApi({ name: 'keys_view', minRole: 'super_admin' }, async () => {
  const check = (...names: string[]) =>
    names.reduce<Record<string, boolean>>((acc, n) => ({ ...acc, [n]: Boolean(process.env[n]) }), {});
  return ok({
    render: check('RENDER_API_KEY', 'RENDER_SERVICE_ID', 'RENDER_URL'),
    supabase: check('NEXT_PUBLIC_SUPABASE_URL', 'NEXT_PUBLIC_SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY'),
    github: check('GITHUB_TOKEN', 'GITHUB_OWNER', 'GITHUB_REPO'),
    vercel: check('VERCEL_TOKEN', 'VERCEL_PROJECT_ID'),
    runtime: check('SESSION_SECRET', 'WEBHOOK_SECRET'),
  });
});

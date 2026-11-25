import { cookies } from 'next/headers';
import { redirect } from 'next/navigation';
import { verifySession, SESSION_COOKIE } from '@/lib/session';
import { requireService } from '@/lib/supabase';
import Nav from '@/components/nav';

export const dynamic = 'force-dynamic';

export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const store = await cookies();
  const payload = verifySession(store.get(SESSION_COOKIE)?.value);
  if (!payload) redirect('/login');

  let role = payload.role;
  let enabled = true;
  try {
    const sb = requireService();
    const { data } = await sb.from('dashboard_users').select('role,enabled').eq('username', payload.sub).maybeSingle();
    if (data) {
      role = String(data.role || role);
      enabled = Number(data.enabled) === 1;
    }
  } catch {
    /* keep token role */
  }
  if (!enabled) redirect('/login');

  // فصل البيئات: شارة واضحة على نشرات المعاينة — التغييرات هناك للقراءة افتراضياً
  const vercelEnv = String(process.env.VERCEL_ENV || 'development');
  const isPreview = vercelEnv === 'preview';

  return (
    <div className="flex min-h-screen flex-col">
      {isPreview && (
        <div className="bg-panel-warn/15 px-4 py-1.5 text-center text-xs font-bold text-panel-warn" role="status">
          بيئة معاينة (Preview) — للقراءة فقط افتراضياً؛ لا تلمس الإنتاج أو المحرك الحي. الإنتاج: telegram-br-7.vercel.app
        </div>
      )}
      <Nav email={payload.sub} role={role} />
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">{children}</main>
      <footer className="mt-auto border-t border-panel-border py-3 text-center text-xs text-panel-muted">
        BR7 Control Plane — Render + Supabase + GitHub + Vercel
      </footer>
    </div>
  );
}

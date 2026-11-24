'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { hasMin, Role } from '@/lib/auth';
import { api } from './ui';

const LINKS: Array<{ href: string; label: string; min: Role }> = [
  { href: '/', label: 'الرئيسية', min: 'viewer' },
  { href: '/database', label: 'قاعدة البيانات', min: 'viewer' },
  { href: '/render', label: 'Render', min: 'viewer' },
  { href: '/github', label: 'GitHub', min: 'viewer' },
  { href: '/deploy', label: 'النشر', min: 'viewer' },
  { href: '/audit', label: 'التدقيق', min: 'viewer' },
  { href: '/settings', label: 'الإعدادات', min: 'super_admin' },
];

export default function Nav({ email, role }: { email: string; role: string }) {
  const path = usePathname();
  const router = useRouter();
  const logout = async () => {
    await api('/api/auth/logout', { method: 'POST' });
    router.replace('/login');
  };
  return (
    <header className="border-b border-panel-border bg-panel-surface">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
        <span className="text-sm font-black tracking-tight text-panel-accent">BR7 Control Plane</span>
        <nav className="flex flex-wrap items-center gap-1" aria-label="التنقل الرئيسي">
          {LINKS.map((l) => {
            const active = l.href === '/' ? path === '/' : path.startsWith(l.href);
            const allowed = hasMin(role, l.min);
            return (
              <Link
                key={l.href}
                href={l.href}
                aria-current={active ? 'page' : undefined}
                className={`rounded-lg px-3 py-1.5 text-sm ${active ? 'bg-panel-accent/15 text-panel-accent font-bold' : allowed ? 'text-panel-muted hover:text-panel-text' : 'text-panel-muted/40'}`}
              >
                {l.label}
              </Link>
            );
          })}
        </nav>
        <div className="ms-auto flex items-center gap-3">
          <span className="text-xs text-panel-muted">
            {email} · <span className="text-panel-accent">{role}</span>
          </span>
          <button onClick={logout} className="rounded-lg border border-panel-border px-3 py-1.5 text-xs text-panel-muted hover:text-panel-text">
            خروج
          </button>
        </div>
      </div>
    </header>
  );
}

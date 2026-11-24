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

const BOT_LINKS: Array<{ href: string; label: string; min: Role }> = [
  { href: '/bot', label: 'نظرة عامة', min: 'viewer' },
  { href: '/bot/control', label: 'التحكم بالتشغيل', min: 'viewer' },
  { href: '/bot/accounts', label: 'الحسابات', min: 'viewer' },
  { href: '/bot/sources', label: 'المصادر', min: 'viewer' },
  { href: '/bot/keywords', label: 'الكلمات', min: 'viewer' },
  { href: '/bot/engine', label: 'محرك التصفية', min: 'viewer' },
  { href: '/bot/rules', label: 'القواعد', min: 'viewer' },
  { href: '/bot/alerts', label: 'التنبيهات', min: 'viewer' },
  { href: '/bot/blocked', label: 'الحظر', min: 'viewer' },
  { href: '/bot/quality', label: 'الجودة', min: 'viewer' },
  { href: '/bot/performance', label: 'الأداء', min: 'viewer' },
  { href: '/bot/settings', label: 'الإعدادات', min: 'viewer' },
  { href: '/bot/commands', label: 'الأوامر', min: 'viewer' },
  { href: '/bot/audit', label: 'سجل العمليات', min: 'viewer' },
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
        <nav className="flex w-full flex-wrap items-center gap-1 border-t border-panel-border/50 pt-2" aria-label="مركز التحكم بالبوت">
          <span className="me-2 rounded-md bg-panel-accent/10 px-2 py-1 text-xs font-black text-panel-accent">مركز التحكم بالبوت</span>
          {BOT_LINKS.map((l) => {
            const active = path === l.href;
            return (
              <Link
                key={l.href}
                href={l.href}
                aria-current={active ? 'page' : undefined}
                className={`rounded-lg px-2.5 py-1 text-xs ${active ? 'bg-panel-accent/15 text-panel-accent font-bold' : 'text-panel-muted hover:text-panel-text'}`}
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
          <Link href="/account" className="rounded-lg border border-panel-border px-3 py-1.5 text-xs text-panel-muted hover:text-panel-text">حسابي</Link>
          <button onClick={logout} className="rounded-lg border border-panel-border px-3 py-1.5 text-xs text-panel-muted hover:text-panel-text">
            خروج
          </button>
        </div>
      </div>
    </header>
  );
}

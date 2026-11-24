'use client';

import React from 'react';
import { hasMin, Role } from '@/lib/auth';

export type ApiEnvelope<T> = { ok: true; data: T } | { ok: false; error: string; code?: string };

export async function api<T>(path: string, init?: RequestInit): Promise<ApiEnvelope<T>> {
  const res = await fetch(path, {
    credentials: 'same-origin',
    ...init,
    headers: { 'Content-Type': 'application/json', ...((init?.headers as Record<string, string>) || {}) },
  });
  try {
    return (await res.json()) as ApiEnvelope<T>;
  } catch {
    return { ok: false, error: `http_${res.status}` };
  }
}

export function useApi<T>(path: string | null, pollMs?: number) {
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const load = React.useCallback(async () => {
    if (!path) return;
    const r = await api<T>(path);
    if (r.ok) {
      setData(r.data);
      setError(null);
    } else {
      setError(r.error);
    }
    setLoading(false);
  }, [path]);
  React.useEffect(() => {
    load();
    if (!pollMs) return;
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [load, pollMs]);
  return { data, error, loading, reload: load };
}

export function useMe() {
  const { data, loading } = useApi<{ email: string; role: string; enabled: boolean }>('/api/auth/me');
  return { me: data, loading };
}

export function Gate({ role, min, children }: { role?: string; min: Role; children: React.ReactNode }) {
  if (!role) return null;
  if (!hasMin(role, min)) {
    return <div className="rounded-lg border border-panel-border bg-panel-surface p-4 text-sm text-panel-muted">صلاحية غير كافية — مطلوب: {min}</div>;
  }
  return <>{children}</>;
}

export function Card({ title, children, actions, className }: { title?: React.ReactNode; children: React.ReactNode; actions?: React.ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-panel-border bg-panel-surface p-4 ${className || ''}`}>
      {(title || actions) && (
        <div className="mb-3 flex items-center justify-between gap-3">
          {title && <h2 className="text-sm font-bold text-panel-text">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Btn({
  children,
  onClick,
  variant = 'primary',
  disabled,
  type = 'button',
  className,
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: 'primary' | 'ghost' | 'danger' | 'ok';
  disabled?: boolean;
  type?: 'button' | 'submit';
  className?: string;
}) {
  const styles: Record<string, string> = {
    primary: 'bg-panel-accent text-black hover:bg-panel-accentHover',
    ghost: 'border border-panel-border text-panel-text hover:bg-panel-border/40',
    danger: 'bg-panel-bad text-white hover:opacity-90',
    ok: 'bg-panel-ok text-white hover:opacity-90',
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg px-3 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${styles[variant]} ${className || ''}`}
    >
      {children}
    </button>
  );
}

export function Badge({ tone = 'muted', children }: { tone?: 'ok' | 'warn' | 'bad' | 'muted'; children: React.ReactNode }) {
  const map: Record<string, string> = {
    ok: 'bg-panel-ok/15 text-panel-ok border-panel-ok/30',
    warn: 'bg-panel-warn/15 text-panel-warn border-panel-warn/30',
    bad: 'bg-panel-bad/15 text-panel-bad border-panel-bad/30',
    muted: 'bg-panel-border/40 text-panel-muted border-panel-border',
  };
  return <span className={`inline-block rounded-md border px-2 py-0.5 text-xs font-medium ${map[tone]}`}>{children}</span>;
}

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...props}
      className={`w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-sm text-panel-text placeholder:text-panel-muted/60 focus:border-panel-accent focus:outline-none ${props.className || ''}`}
    />
  );
}

export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      {...props}
      className={`w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-sm text-panel-text focus:border-panel-accent focus:outline-none ${props.className || ''}`}
    />
  );
}

export function TextArea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      className={`w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-sm text-panel-text focus:border-panel-accent focus:outline-none ${props.className || ''}`}
    />
  );
}

export function Modal({ open, title, children, onClose }: { open: boolean; title: string; children: React.ReactNode; onClose: () => void }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={onClose}>
      <div className="w-full max-w-lg rounded-xl border border-panel-border bg-panel-surface p-4" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-sm font-bold">{title}</h3>
          <button onClick={onClose} className="text-panel-muted hover:text-panel-text" aria-label="إغلاق">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Spinner() {
  return <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-panel-muted border-t-transparent" />;
}

export function StateDot({ tone }: { tone: 'ok' | 'warn' | 'bad' | 'muted' }) {
  const map: Record<string, string> = { ok: 'bg-panel-ok', warn: 'bg-panel-warn', bad: 'bg-panel-bad', muted: 'bg-panel-muted' };
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${map[tone]}`} />;
}

export function deployTone(status?: string | null): 'ok' | 'warn' | 'bad' | 'muted' {
  const s = (status || '').toLowerCase();
  if (['live', 'ready', 'success', 'ok'].includes(s)) return 'ok';
  if (['building', 'created', 'updating', 'queued', 'initiating', 'in_progress', 'pending', 'activating', 'deactivating', 'pre_deploy'].includes(s)) return 'warn';
  if (['build_failed', 'activate_failed', 'canceled', 'failure', 'error', 'cancelled', 'timed_out'].includes(s)) return 'bad';
  return 'muted';
}

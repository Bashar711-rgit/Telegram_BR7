'use client';

import React from 'react';
import { Badge, Card } from './ui';

/** Honest mode badge — the truth of every capability in the Bot Control Center. */
export function ModeBadge({ mode }: { mode: 'live' | 'live_persistent' | 'contract' }) {
  if (mode === 'live') return <Badge tone="ok">حي</Badge>;
  if (mode === 'live_persistent') return <Badge tone="warn">حي — مثابر</Badge>;
  return <Badge tone="muted">ينتظر Backend</Badge>;
}

export function ContractNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-panel-border/60 bg-panel-bg p-3 text-xs leading-relaxed text-panel-muted">
      {children}
    </div>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: React.ReactNode; hint?: string; tone?: 'ok' | 'warn' | 'bad' | 'muted' }) {
  const colors: Record<string, string> = {
    ok: 'text-panel-ok',
    warn: 'text-panel-warn',
    bad: 'text-panel-bad',
    muted: 'text-panel-text',
  };
  return (
    <div className="rounded-lg border border-panel-border bg-panel-bg p-3">
      <div className="text-xs text-panel-muted">{label}</div>
      <div className={`mt-1 text-lg font-bold ${colors[tone || 'muted']}`}>{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-panel-muted">{hint}</div>}
    </div>
  );
}

export function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-panel-border/40 py-1.5 last:border-0">
      <span className="shrink-0 text-xs text-panel-muted">{k}</span>
      <span className="break-all text-right text-xs text-panel-text">{children}</span>
    </div>
  );
}

export function ErrorText({ children }: { children?: React.ReactNode }) {
  if (!children) return null;
  return <div className="mt-2 rounded-lg border border-panel-bad/40 bg-panel-bad/10 p-2 text-xs text-panel-bad">{children}</div>;
}

export function OkText({ children }: { children?: React.ReactNode }) {
  if (!children) return null;
  return <div className="mt-2 rounded-lg border border-panel-ok/40 bg-panel-ok/10 p-2 text-xs text-panel-ok">{children}</div>;
}

export function fmtUptime(sec?: number): string {
  if (sec === undefined || sec === null) return '—';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return d > 0 ? `${d}ي ${h}س ${m}د` : h > 0 ? `${h}س ${m}د` : `${m}د`;
}

export const STATE_LABELS: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'muted' }> = {
  running: { label: 'يعمل', tone: 'ok' },
  degraded: { label: 'متدهور', tone: 'warn' },
  starting: { label: 'يبدأ', tone: 'warn' },
  stopping: { label: 'يتوقف', tone: 'warn' },
  restarting: { label: 'يُعاد تشغيله', tone: 'warn' },
  stopped: { label: 'متوقف', tone: 'bad' },
  maintenance: { label: 'صيانة', tone: 'warn' },
  error: { label: 'خطأ', tone: 'bad' },
};

export { Card };

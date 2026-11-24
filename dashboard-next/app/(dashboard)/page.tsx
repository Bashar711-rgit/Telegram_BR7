'use client';

import React from 'react';
import Link from 'next/link';
import { useApi, useMe, Card, Badge, StateDot, Btn, deployTone } from '@/components/ui';

type Overview = {
  ts: string;
  render: { configured: boolean; reachable: boolean; name?: string | null; suspended?: boolean | null; latestDeploy?: Record<string, unknown> | null };
  vercel: { configured: boolean; reachable: boolean; deploys?: Array<Record<string, unknown>> };
  supabase: { configured: boolean; reachable: boolean; latencyMs?: number };
  github: { configured: boolean; reachable: boolean; defaultBranch?: string | null; pushedAt?: string | null; latestRun?: { status?: string; conclusion?: string; name?: string } | null };
  bot: { reachable: boolean; status?: string; dbHealthy?: boolean };
};

function statusTone(v?: boolean | null): 'ok' | 'bad' | 'muted' {
  if (v === true) return 'ok';
  if (v === false) return 'bad';
  return 'muted';
}

export default function OverviewPage() {
  const { data, loading, error } = useApi<Overview>('/api/overview', 30000);
  const { me } = useMe();

  const cards: Array<{ title: string; tone: 'ok' | 'bad' | 'muted'; lines: Array<{ k: string; v: React.ReactNode }> }> = [
    {
      title: 'Render — البوت',
      tone: statusTone(data?.render.reachable),
      lines: [
        { k: 'الخدمة', v: data?.render.name || '—' },
        { k: 'آخر نشر', v: <DeployStatus d={data?.render.latestDeploy} /> },
        { k: 'الرابط', v: <a className="text-panel-accent underline" href="https://telegram-br7.onrender.com/health" target="_blank" rel="noreferrer">/health</a> },
      ],
    },
    {
      title: 'Supabase — قاعدة البيانات',
      tone: statusTone(data?.supabase.reachable),
      lines: [
        { k: 'الحالة', v: data?.supabase.reachable ? 'متصل' : 'غير متصل' },
        { k: 'زمن الاستجابة', v: data?.supabase.latencyMs != null ? `${data.supabase.latencyMs}ms` : '—' },
        { k: 'الجداول', v: '34 (public)' },
      ],
    },
    {
      title: 'Vercel — اللوحة',
      tone: statusTone(data?.vercel.reachable),
      lines: [
        { k: 'الحالة', v: data?.vercel.reachable ? 'متصل' : 'غير متصل' },
        { k: 'آخر نشر', v: <VercelStatus d={(data?.vercel.deploys || [])[0]} /> },
        { k: 'النشرات', v: String(data?.vercel.deploys?.length ?? 0) },
      ],
    },
    {
      title: 'GitHub — المستودع',
      tone: statusTone(data?.github.reachable),
      lines: [
        { k: 'الفرع', v: data?.github.defaultBranch || '—' },
        { k: 'آخر دفع', v: data?.github.pushedAt ? String(data.github.pushedAt).slice(0, 16).replace('T', ' ') : '—' },
        { k: 'CI', v: data?.github.latestRun ? `${data.github.latestRun.name}: ${data.github.latestRun.conclusion || data.github.latestRun.status}` : '—' },
      ],
    },
  ];

  const lastDeploy = data?.render.latestDeploy as Record<string, unknown> | undefined;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-panel-border bg-panel-surface p-4">
        <div className="text-sm">
          <div className="font-bold">آخر نشر للبوت: <span className="text-panel-muted">{String(lastDeploy?.status || '—')}</span></div>
          <div className="text-xs text-panel-muted">
            {lastDeploy?.commitMessage ? String(lastDeploy.commitMessage).slice(0, 80) : '—'} · صحة البوت:{' '}
            {data?.bot.reachable ? `${data.bot.status}${data.bot.dbHealthy ? ' / db_healthy' : ''}` : 'غير متصل'}
          </div>
        </div>
        <Link href="/deploy">
          <Btn className="!px-6 !py-3 text-base font-black">نشر الآن →</Btn>
        </Link>
      </div>

      {error && <p className="rounded-lg border border-panel-bad/30 bg-panel-bad/10 p-3 text-sm text-panel-bad">خطأ في جلب البيانات: {error}</p>}
      {loading && <p className="text-sm text-panel-muted">جارٍ التحميل…</p>}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {cards.map((c) => (
          <Card key={c.title} title={<span className="flex items-center gap-2"><StateDot tone={c.tone} /> {c.title}</span>}>
            <dl className="space-y-2 text-sm">
              {c.lines.map((l) => (
                <div key={l.k} className="flex justify-between gap-4 border-b border-panel-border/50 pb-1 last:border-0">
                  <dt className="text-panel-muted">{l.k}</dt>
                  <dd className="text-left text-panel-text">{l.v}</dd>
                </div>
              ))}
            </dl>
          </Card>
        ))}
      </div>

      <p className="text-xs text-panel-muted">
        المستخدم الحالي: {me?.email} ({me?.role}) · التحديث التلقائي كل 30 ثانية · دورة صلاحيات: viewer → operator → admin → super_admin
      </p>
    </div>
  );
}

function DeployStatus({ d }: { d?: Record<string, unknown> | null }) {
  if (!d) return <>—</>;
  return (
    <span className="inline-flex items-center gap-1">
      <Badge tone={deployTone(String(d.status || ''))}>{String(d.status || '—')}</Badge>
      <span className="text-xs text-panel-muted">{String(d.commitId || '').slice(0, 7)}</span>
    </span>
  );
}

function VercelStatus({ d }: { d?: Record<string, unknown> | null }) {
  if (!d) return <>—</>;
  return (
    <span className="inline-flex items-center gap-1">
      <Badge tone={deployTone(String(d.state || ''))}>{String(d.target || 'preview')}:{String(d.state || '—')}</Badge>
      <span className="text-xs text-panel-muted">{String(d.branch || '')}@{String(d.sha || '')}</span>
    </span>
  );
}

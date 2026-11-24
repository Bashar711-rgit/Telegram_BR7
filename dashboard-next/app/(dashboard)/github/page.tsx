'use client';

import React from 'react';
import { api, useApi, useMe, Btn, Card, Badge, Input, Spinner, deployTone, Gate } from '@/components/ui';

type ReleasesInfo = { configured: boolean; reachable: boolean; releases?: Array<Record<string, unknown>> };
type RunsInfo = { configured: boolean; reachable: boolean; total?: number; runs?: Array<Record<string, unknown>> };
type CommitsInfo = { configured: boolean; reachable: boolean; commits?: Array<Record<string, unknown>> };

export default function GithubPage() {
  const releases = useApi<ReleasesInfo>('/api/github/releases');
  const runs = useApi<RunsInfo>('/api/github/runs?limit=10', 60000);
  const commits = useApi<CommitsInfo>('/api/github/commits');
  const { me } = useMe();
  const role = me?.role || '';
  const canAdmin = ['admin', 'super_admin'].includes(role);

  const [tag, setTag] = React.useState('');
  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const createTag = async () => {
    if (!tag.trim()) return;
    setBusy(true);
    const r = await api<{ tag: string }>('/api/github/tag', { method: 'POST', body: JSON.stringify({ tag: tag.trim() }) });
    setBusy(false);
    setNotice(r.ok ? `تم إنشاء tag: ${(r as { data: { tag: string } }).data.tag}` : `فشل: ${r.error}`);
    if (r.ok) setTag('');
  };

  const rerun = async (id: string) => {
    setBusy(true);
    const r = await api(`/api/github/runs/${id}/rerun`, { method: 'POST' });
    setBusy(false);
    setNotice(r.ok ? `أُعيد تشغيل run ${id}` : `فشل: ${r.error}`);
  };

  return (
    <div className="space-y-4">
      {notice && <p className="rounded-lg border border-panel-accent/30 bg-panel-accent/10 p-3 text-sm text-panel-accent">{notice}</p>}

      <Card title="إنشاء Tag (على رأس main)">
        <Gate role={role} min="admin">
          <div className="flex gap-2">
            <Input placeholder="مثال: v14.9.0 أو test-tag-20261124" value={tag} onChange={(e) => setTag(e.target.value)} className="!w-72" />
            <Btn onClick={createTag} disabled={busy || !tag.trim()}>إنشاء</Btn>
          </div>
        </Gate>
      </Card>

      <Card title="Releases">
        {releases.loading ? <Spinner /> : (
          <div className="max-h-72 overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg text-panel-muted"><tr><th className="border-b border-panel-border px-3 py-2 text-left">tag</th><th className="border-b border-panel-border px-3 py-2 text-left">name</th><th className="border-b border-panel-border px-3 py-2 text-left">published</th></tr></thead>
              <tbody>
                {(releases.data?.releases || []).map((r) => (
                  <tr key={String(r.tagName)}>
                    <td className="border-b border-panel-border/50 px-3 py-1.5 font-medium">{String(r.tagName)}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.name || '—')}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.publishedAt || '').slice(0, 10)}</td>
                  </tr>
                ))}
                {(releases.data?.releases || []).length === 0 && <tr><td colSpan={3} className="px-3 py-4 text-center text-panel-muted">لا releases</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Workflow Runs (CI)">
        {runs.loading ? <Spinner /> : (
          <div className="max-h-80 overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg text-panel-muted"><tr><th className="border-b border-panel-border px-3 py-2 text-left">id</th><th className="border-b border-panel-border px-3 py-2 text-left">name</th><th className="border-b border-panel-border px-3 py-2 text-left">status</th><th className="border-b border-panel-border px-3 py-2 text-left">conclusion</th><th className="border-b border-panel-border px-3 py-2 text-left">branch</th><th className="border-b border-panel-border px-3 py-2 text-left">actions</th></tr></thead>
              <tbody>
                {(runs.data?.runs || []).map((r) => (
                  <tr key={String(r.id)}>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.id)}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.name || '')}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5"><Badge tone="warn">{String(r.status || '')}</Badge></td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5"><Badge tone={deployTone(String(r.conclusion || ''))}>{String(r.conclusion || '—')}</Badge></td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.headBranch || '')}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">
                      {canAdmin && ['failure', 'cancelled', 'timed_out'].includes(String(r.conclusion)) && (
                        <button className="text-panel-accent hover:underline" onClick={() => rerun(String(r.id))}>rerun</button>
                      )}
                    </td>
                  </tr>
                ))}
                {(runs.data?.runs || []).length === 0 && <tr><td colSpan={6} className="px-3 py-4 text-center text-panel-muted">لا runs</td></tr>}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="Commits (آخر 10)">
        {commits.loading ? <Spinner /> : (
          <ul className="max-h-72 space-y-1 overflow-auto rounded-lg border border-panel-border p-3 text-xs" dir="ltr">
            {(commits.data?.commits || []).map((c) => (
              <li key={String(c.sha)} className="flex items-start gap-2 border-b border-panel-border/40 py-1 last:border-0">
                <code className="text-panel-accent">{String(c.sha)}</code>
                <span className="flex-1 text-panel-text">{String(c.message || '')}</span>
                <span className="text-panel-muted">{String(c.author || '')}</span>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

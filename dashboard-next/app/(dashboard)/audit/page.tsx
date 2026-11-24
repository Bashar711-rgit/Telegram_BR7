'use client';

import React from 'react';
import { useApi, Btn, Card, Input, Spinner } from '@/components/ui';

type AuditRow = Record<string, unknown>;

const PAGE_SIZE = 50;

export default function AuditPage() {
  const [page, setPage] = React.useState(0);
  const [search, setSearch] = React.useState('');
  const [query, setQuery] = React.useState('');

  const { data, loading, error } = useApi<{ count: number; rows: AuditRow[] }>(
    `/api/audit?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}${query ? `&search=${encodeURIComponent(query)}` : ''}`,
  );

  const exportCsv = async () => {
    const url = `/api/audit?limit=200${query ? `&search=${encodeURIComponent(query)}` : ''}&format=csv`;
    const res = await fetch(url, { credentials: 'same-origin' });
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'br7_audit.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  };

  return (
    <div className="space-y-4">
      <Card
        title="سجل التدقيق الموحد (البوت + اللوحة)"
        actions={
          <div className="flex items-center gap-2">
            <Input
              placeholder="بحث في action/actor/object_type"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  setPage(0);
                  setQuery(search.trim());
                }
              }}
              className="!w-64"
            />
            <Btn variant="ghost" onClick={() => { setPage(0); setQuery(search.trim()); }}>بحث</Btn>
            <Btn variant="ghost" onClick={exportCsv}>تصدير CSV</Btn>
          </div>
        }
      >
        {error && <p className="rounded-lg border border-panel-bad/30 bg-panel-bad/10 p-2 text-xs text-panel-bad">{error}</p>}
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-panel-muted"><Spinner /> جارٍ التحميل…</div>
        ) : (
          <div className="max-h-96 overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg text-panel-muted">
                <tr>
                  <th className="border-b border-panel-border px-3 py-2 text-left">id</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">created_at</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">actor</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">action</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">object</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">source</th>
                </tr>
              </thead>
              <tbody>
                {(data?.rows || []).map((r) => (
                  <tr key={String(r.id)} className="hover:bg-panel-border/20">
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.id)}</td>
                    <td className="whitespace-nowrap border-b border-panel-border/50 px-3 py-1.5">{String(r.created_at || '').slice(0, 19).replace('T', ' ')}</td>
                    <td className="max-w-40 truncate border-b border-panel-border/50 px-3 py-1.5" title={String(r.actor)}>{String(r.actor)}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5 font-medium text-panel-accent">{String(r.action)}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(r.object_type || '')}:{String(r.object_id || '')}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5 text-panel-muted">{String(r.source || '')}</td>
                  </tr>
                ))}
                {(data?.rows || []).length === 0 && (
                  <tr><td colSpan={6} className="px-3 py-6 text-center text-panel-muted">لا نتائج</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex items-center justify-between text-xs text-panel-muted">
          <span>صفحة {page + 1} · {data?.count ?? 0} صف</span>
          <div className="flex gap-2">
            <Btn variant="ghost" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>السابق</Btn>
            <Btn variant="ghost" disabled={(data?.count ?? 0) < PAGE_SIZE} onClick={() => setPage((p) => p + 1)}>التالي</Btn>
          </div>
        </div>
      </Card>
    </div>
  );
}

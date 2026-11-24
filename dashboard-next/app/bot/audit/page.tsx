'use client';

import React from 'react';
import { useApi, Card, Btn, Input, Badge } from '@/components/ui';
import { ErrorText } from '@/components/bot';

type AuditRow = {
  id: number;
  actor: string;
  action: string;
  object_type: string | null;
  object_id: string | null;
  old_value: string | null;
  new_value: string | null;
  source: string | null;
  created_at: string;
  ip: string | null;
  request_id: string | null;
};

export default function BotAudit() {
  const [search, setSearch] = React.useState('');
  const [offset, setOffset] = React.useState(0);
  const limit = 50;
  const qs = `limit=${limit}&offset=${offset}&order=id&desc=1${search ? `&search=${encodeURIComponent(search)}` : ''}`;
  const { data, error, loading } = useApi<{ count: number; rows: AuditRow[] }>(`/api/audit?${qs}`);

  const [detail, setDetail] = React.useState<AuditRow | null>(null);

  const isBotAction = (a: string) => a.startsWith('bot_') || a.startsWith('keywords_') || a.startsWith('filter_profile') || a.startsWith('dashboard_');

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">سجل العمليات والتدقيق</h1>
        <div className="flex gap-2">
          <a href="/api/audit?format=csv&limit=200" className="rounded-lg border border-panel-border px-3 py-2 text-xs text-panel-text hover:bg-panel-border/40">تصدير CSV</a>
          <Btn variant="ghost" onClick={() => window.location.reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      <Card title={`التدقيق (${data?.count ?? 0} صف في هذه الصفحة) — غير قابل للتعديل من المستخدمين`}>
        <Input
          placeholder="بحث: action / actor / object_type (مثال: bot_command، keywords، dashboard_user)"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setOffset(0); }}
          className="mb-3"
        />
        {loading && !data ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : (data?.rows.length ?? 0) === 0 ? (
          <div className="text-sm text-panel-muted">لا صفوف مطابقة</div>
        ) : (
          <div className="max-h-96 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-panel-surface">
                <tr className="text-panel-muted">
                  <th className="p-2 text-right">#</th>
                  <th className="p-2 text-right">الوقت</th>
                  <th className="p-2 text-right">الفاعل</th>
                  <th className="p-2 text-right">الإجراء</th>
                  <th className="p-2 text-right">الهدف</th>
                  <th className="p-2 text-right">المصدر</th>
                  <th className="p-2"></th>
                </tr>
              </thead>
              <tbody>
                {data!.rows.map((r) => (
                  <tr key={r.id} className="border-t border-panel-border/40 hover:bg-panel-border/20">
                    <td className="p-2">{r.id}</td>
                    <td className="p-2">{new Date(r.created_at).toLocaleString('ar')}</td>
                    <td className="p-2 max-w-40 truncate" dir="ltr">{r.actor}</td>
                    <td className="p-2">
                      {isBotAction(r.action) ? <Badge tone="warn">{r.action}</Badge> : <span dir="ltr">{r.action}</span>}
                    </td>
                    <td className="p-2 max-w-40 truncate" dir="ltr">{r.object_type ? `${r.object_type}${r.object_id ? `#${r.object_id}` : ''}` : '—'}</td>
                    <td className="p-2 text-panel-muted" dir="ltr">{r.source || '—'}</td>
                    <td className="p-2"><Btn variant="ghost" onClick={() => setDetail(r)}>تفاصيل</Btn></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex items-center justify-between text-xs">
          <Btn variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>السابق</Btn>
          <span className="text-panel-muted" dir="ltr">offset {offset}</span>
          <Btn variant="ghost" disabled={(data?.count ?? 0) < limit} onClick={() => setOffset(offset + limit)}>التالي</Btn>
        </div>
      </Card>

      {detail && (
        <Card title={`تفاصيل السجل #${detail.id}`} actions={<Btn variant="ghost" onClick={() => setDetail(null)}>إغلاق</Btn>}>
          <div className="space-y-2 text-xs">
            <div><span className="text-panel-muted">الفاعل:</span> <span dir="ltr">{detail.actor}</span> · <span className="text-panel-muted">IP:</span> <span dir="ltr">{detail.ip || '—'}</span></div>
            <div><span className="text-panel-muted">الإجراء:</span> <span dir="ltr">{detail.action}</span> · <span className="text-panel-muted">request_id:</span> <span dir="ltr">{detail.request_id || '—'}</span></div>
            <div><span className="text-panel-muted">الهدف:</span> <span dir="ltr">{detail.object_type || '—'}{detail.object_id ? `#${detail.object_id}` : ''}</span></div>
            <div>
              <div className="text-panel-muted">القيمة السابقة (مموّه):</div>
              <pre className="max-h-40 overflow-auto rounded-lg border border-panel-border bg-panel-bg p-2" dir="ltr" style={{ scrollbarWidth: 'thin' }}>{detail.old_value || 'null'}</pre>
            </div>
            <div>
              <div className="text-panel-muted">القيمة الجديدة (مموّه):</div>
              <pre className="max-h-40 overflow-auto rounded-lg border border-panel-border bg-panel-bg p-2" dir="ltr" style={{ scrollbarWidth: 'thin' }}>{detail.new_value || 'null'}</pre>
            </div>
          </div>
        </Card>
      )}
    </div>
  );
}

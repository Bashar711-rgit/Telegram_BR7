'use client';

import React from 'react';
import { useParams } from 'next/navigation';
import { api, useMe, Btn, Card, Badge, Modal, Input, TextArea, Spinner, Gate } from '@/components/ui';

type Row = Record<string, unknown>;
type FieldDef = { name: string; label: string; type: 'text' | 'int' | 'flag' | 'textarea' };

const CONFIG: Record<string, { label: string; pk: string; create?: FieldDef[]; edit?: FieldDef[]; note?: string }> = {
  rules: {
    label: 'القواعد', pk: 'id',
    create: [
      { name: 'name', label: 'الاسم', type: 'text' },
      { name: 'conditions', label: 'الشروط (JSON)', type: 'textarea' },
      { name: 'action', label: 'الإجراء', type: 'text' },
      { name: 'action_value', label: 'قيمة الإجراء (اختياري)', type: 'text' },
      { name: 'priority', label: 'الأولوية', type: 'int' },
      { name: 'enabled', label: 'مفعلة (0/1)', type: 'flag' },
    ],
    edit: [
      { name: 'name', label: 'الاسم', type: 'text' },
      { name: 'conditions', label: 'الشروط (JSON)', type: 'textarea' },
      { name: 'action', label: 'الإجراء', type: 'text' },
      { name: 'action_value', label: 'قيمة الإجراء', type: 'text' },
      { name: 'priority', label: 'الأولوية', type: 'int' },
      { name: 'enabled', label: 'مفعلة (0/1)', type: 'flag' },
    ],
  },
  sources: {
    label: 'المصادر', pk: 'id',
    create: [
      { name: 'chat_id', label: 'Chat ID', type: 'int' },
      { name: 'username', label: 'المعرف (اختياري)', type: 'text' },
      { name: 'title', label: 'العنوان (اختياري)', type: 'text' },
      { name: 'type', label: 'النوع', type: 'text' },
      { name: 'notes', label: 'ملاحظات', type: 'text' },
      { name: 'enabled', label: 'مفعل (0/1)', type: 'flag' },
    ],
    edit: [
      { name: 'title', label: 'العنوان', type: 'text' },
      { name: 'username', label: 'المعرف', type: 'text' },
      { name: 'enabled', label: 'مفعل (0/1)', type: 'flag' },
      { name: 'notes', label: 'ملاحظات', type: 'text' },
    ],
  },
  blocked_senders: {
    label: 'المحظورون', pk: 'sender_id',
    create: [
      { name: 'sender_id', label: 'Sender ID', type: 'int' },
      { name: 'reason', label: 'السبب', type: 'text' },
    ],
    edit: [{ name: 'reason', label: 'السبب', type: 'text' }],
  },
  allowed_entities: {
    label: 'المسموحون', pk: 'id',
    create: [
      { name: 'entity_type', label: 'النوع', type: 'text' },
      { name: 'entity_id', label: 'Entity ID', type: 'int' },
      { name: 'note', label: 'ملاحظة', type: 'text' },
    ],
    edit: [{ name: 'note', label: 'ملاحظة', type: 'text' }],
  },
  app_settings: {
    label: 'الإعدادات (KV)', pk: 'key',
    create: [
      { name: 'key', label: 'المفتاح', type: 'text' },
      { name: 'value', label: 'القيمة', type: 'textarea' },
    ],
    edit: [{ name: 'value', label: 'القيمة', type: 'textarea' }],
    note: 'قيم هذا الجدول تظهر مموّهة لغير المدراء',
  },
  panel_accounts: {
    label: 'حسابات التشغيل', pk: 'id',
    edit: [
      { name: 'display_name', label: 'الاسم المعروض', type: 'text' },
      { name: 'status', label: 'الحالة', type: 'text' },
      { name: 'monitoring_enabled', label: 'المراقبة (0/1)', type: 'flag' },
      { name: 'priority', label: 'الأولوية', type: 'int' },
      { name: 'is_main', label: 'رئيسي (0/1)', type: 'flag' },
    ],
    note: 'لا يمكن إنشاء حسابات من اللوحة — الربط يتم عبر البوت. لا تُعرض مفاتيح api_hash/session مطلقاً',
  },
  messages: { label: 'الرسائل', pk: 'id' },
  alerts: { label: 'التنبيهات', pk: 'id' },
  filter_decisions: { label: 'قرارات الفلترة', pk: 'id' },
  audit_logs: { label: 'سجل التدقيق', pk: 'id' },
};

const PAGE_SIZE = 25;

// v17.2.0: جداول يستوي منحها قراءة اللوحة من الزر مباشرة (بلا SQL يدوي على Supabase)
const GRANTABLE = ['sender_stats', 'blocked_chats', 'notifications'];

export default function EntityPage() {
  const params = useParams<{ entity: string }>();
  const entity = String(params.entity || '');
  const cfg = CONFIG[entity];
  const { me } = useMe();
  const role = me?.role || '';

  const [rows, setRows] = React.useState<Row[]>([]);
  const [page, setPage] = React.useState(0);
  const [filter, setFilter] = React.useState('');
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  const [createOpen, setCreateOpen] = React.useState(false);
  const [createForm, setCreateForm] = React.useState<Record<string, string>>({});
  const [editRow, setEditRow] = React.useState<Row | null>(null);
  const [editForm, setEditForm] = React.useState<Record<string, string>>({});
  const [deleteRow, setDeleteRow] = React.useState<Row | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [grantBusy, setGrantBusy] = React.useState(false);

  const canWrite = ['admin', 'super_admin'].includes(role);

  const load = React.useCallback(async () => {
    if (!entity) return;
    setLoading(true);
    const f = filter.trim() ? `&filter=${encodeURIComponent(filter.trim().replace('=', ':'))}` : '';
    const r = await api<{ rows: Row[]; count: number }>(`/api/db/${entity}?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}&desc=1${f}`);
    if (r.ok) {
      setRows((r as { data: { rows: Row[] } }).data.rows);
      setError(null);
    } else {
      setError(r.error);
    }
    setLoading(false);
  }, [entity, page, filter]);

  React.useEffect(() => {
    load();
  }, [load]);

  if (!cfg) {
    return <Card title="كيان غير معروف"><p className="text-sm text-panel-muted">لا يوجد كيان باسم: {entity}</p></Card>;
  }

  const doCreate = async () => {
    setBusy(true);
    const body: Record<string, unknown> = {};
    for (const f of cfg.create || []) {
      const v = createForm[f.name];
      if (v === undefined || v === '') continue;
      if (f.type === 'int') body[f.name] = Number(v);
      else if (f.type === 'flag') body[f.name] = Number(v);
      else body[f.name] = v;
    }
    const r = await api(`/api/db/${entity}`, { method: 'POST', body: JSON.stringify(body) });
    setBusy(false);
    if (r.ok) {
      setCreateOpen(false);
      setCreateForm({});
      setNotice('تم الإنشاء والتدقيق ✓');
      load();
    } else {
      setNotice(`فشل الإنشاء: ${r.error}`);
    }
  };

  const doEdit = async () => {
    if (!editRow) return;
    setBusy(true);
    const body: Record<string, unknown> = {};
    for (const f of cfg.edit || []) {
      const v = editForm[f.name];
      if (v === undefined) continue;
      if (f.type === 'int' || f.type === 'flag') body[f.name] = Number(v);
      else body[f.name] = v;
    }
    const r = await api(`/api/db/${entity}/${encodeURIComponent(String(editRow[cfg.pk]))}`, { method: 'PATCH', body: JSON.stringify(body) });
    setBusy(false);
    if (r.ok) {
      setEditRow(null);
      setNotice('تم التعديل والتدقيق ✓');
      load();
    } else {
      setNotice(`فشل التعديل: ${r.error}`);
    }
  };

  const doDelete = async () => {
    if (!deleteRow) return;
    setBusy(true);
    const r = await api(`/api/db/${entity}/${encodeURIComponent(String(deleteRow[cfg.pk]))}`, { method: 'DELETE' });
    setBusy(false);
    if (r.ok) {
      setDeleteRow(null);
      setNotice('تم الحذف والتدقيق ✓');
      load();
    } else {
      setNotice(`فشل الحذف: ${r.error}`);
    }
  };

  // v17.2.0: منح قراءة حي عبر قاعدة المحرك (GRANT SELECT بقائمة بيضاء)
  const doGrant = async () => {
    setGrantBusy(true);
    const r = await api<{ results: Array<{ table: string; granted: boolean; error?: string }>; allGranted: boolean }>('/api/db/grant-read', {
      method: 'POST',
      body: JSON.stringify({ tables: [entity] }),
    });
    setGrantBusy(false);
    if (r.ok && (r as { data: { allGranted: boolean } }).data.allGranted) {
      setNotice('نُفّذ GRANT SELECT فعلياً — جارٍ إعادة القراءة…');
      load();
    } else {
      const detail = r.ok
        ? ((r as { data: { results: Array<{ error?: string }> } }).data.results.map((x) => x.error).filter(Boolean).join(' · ') || 'فشل بلا تفصيل')
        : r.error;
      setNotice(`فشل المنح بصدق: ${detail}`);
    }
  };

  const cols = rows.length > 0 ? Object.keys(rows[0]).slice(0, 8) : [];

  return (
    <div className="space-y-4">
      <Card
        title={`${cfg.label} — /api/db/${entity}`}
        actions={
          <div className="flex items-center gap-2">
            {cfg.note && <span className="text-xs text-panel-muted">{cfg.note}</span>}
            <Input placeholder="filter col:value" value={filter} onChange={(e) => setFilter(e.target.value)} className="!w-44" onKeyDown={(e) => { if (e.key === 'Enter') { setPage(0); load(); } }} />
            {cfg.create && canWrite && <Btn onClick={() => setCreateOpen(true)}>+ إضافة</Btn>}
            {GRANTABLE.includes(entity) && role === 'super_admin' && (
              <Btn variant="ghost" disabled={grantBusy} onClick={doGrant}>{grantBusy ? 'جارٍ المنح…' : 'منح قراءة (GRANT حي)'}</Btn>
            )}
            <Btn variant="ghost" onClick={load}>تحديث</Btn>
          </div>
        }
      >
        {notice && <p className="mb-3 rounded-lg border border-panel-accent/30 bg-panel-accent/10 p-2 text-xs text-panel-accent">{notice}</p>}
        {error && <p className="mb-3 rounded-lg border border-panel-bad/30 bg-panel-bad/10 p-2 text-xs text-panel-bad">{error}</p>}
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-panel-muted"><Spinner /> جارٍ التحميل…</div>
        ) : (
          <div className="max-h-96 overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-left text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg">
                <tr className="text-panel-muted">
                  {cols.map((c) => (
                    <th key={c} className="whitespace-nowrap border-b border-panel-border px-3 py-2 font-medium">{c}</th>
                  ))}
                  {canWrite && cfg.edit && <th className="border-b border-panel-border px-3 py-2">إجراءات</th>}
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} className="hover:bg-panel-border/20">
                    {cols.map((c) => (
                      <td key={c} className="max-w-40 truncate whitespace-nowrap border-b border-panel-border/50 px-3 py-1.5 text-panel-text" title={String(r[c] ?? '')}>
                        {r[c] === null || r[c] === undefined ? '—' : String(r[c]).slice(0, 80)}
                      </td>
                    ))}
                    {canWrite && cfg.edit && (
                      <td className="whitespace-nowrap border-b border-panel-border/50 px-3 py-1.5">
                        <button className="text-panel-accent hover:underline" onClick={() => { setEditRow(r); setEditForm(Object.fromEntries((cfg.edit || []).map((f) => [f.name, r[f.name] === null || r[f.name] === undefined ? '' : String(r[f.name])]))); }}>تعديل</button>
                        <span className="mx-2 text-panel-border">|</span>
                        <button className="text-panel-bad hover:underline" onClick={() => setDeleteRow(r)}>حذف</button>
                      </td>
                    )}
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr><td colSpan={cols.length + 1} className="px-3 py-6 text-center text-panel-muted">لا صفوف</td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex items-center justify-between text-xs text-panel-muted">
          <span>صفحة {page + 1} · {PAGE_SIZE} صف/صفحة</span>
          <div className="flex gap-2">
            <Btn variant="ghost" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>السابق</Btn>
            <Btn variant="ghost" disabled={rows.length < PAGE_SIZE} onClick={() => setPage((p) => p + 1)}>التالي</Btn>
          </div>
        </div>
      </Card>

      <Modal open={createOpen} title={`إضافة — ${cfg.label}`} onClose={() => setCreateOpen(false)}>
        <div className="space-y-3">
          {(cfg.create || []).map((f) => f.type === 'textarea' ? (
            <div key={f.name}>
              <label className="mb-1 block text-xs text-panel-muted">{f.label}</label>
              <TextArea rows={4} value={createForm[f.name] || ''} onChange={(e) => setCreateForm((s) => ({ ...s, [f.name]: e.target.value }))} />
            </div>
          ) : (
            <div key={f.name}>
              <label className="mb-1 block text-xs text-panel-muted">{f.label}</label>
              <Input value={createForm[f.name] || ''} onChange={(e) => setCreateForm((s) => ({ ...s, [f.name]: e.target.value }))} />
            </div>
          ))}
          <Btn onClick={doCreate} disabled={busy} className="w-full">{busy ? 'جارٍ…' : 'إنشاء'}</Btn>
        </div>
      </Modal>

      <Modal open={editRow !== null} title={`تعديل — ${cfg.label}`} onClose={() => setEditRow(null)}>
        <div className="space-y-3">
          {(cfg.edit || []).map((f) => f.type === 'textarea' ? (
            <div key={f.name}>
              <label className="mb-1 block text-xs text-panel-muted">{f.label}</label>
              <TextArea rows={4} value={editForm[f.name] ?? ''} onChange={(e) => setEditForm((s) => ({ ...s, [f.name]: e.target.value }))} />
            </div>
          ) : (
            <div key={f.name}>
              <label className="mb-1 block text-xs text-panel-muted">{f.label}</label>
              <Input value={editForm[f.name] ?? ''} onChange={(e) => setEditForm((s) => ({ ...s, [f.name]: e.target.value }))} />
            </div>
          ))}
          <Btn onClick={doEdit} disabled={busy} className="w-full">{busy ? 'جارٍ…' : 'حفظ'}</Btn>
        </div>
      </Modal>

      <Modal open={deleteRow !== null} title="تأكيد الحذف" onClose={() => setDeleteRow(null)}>
        <p className="mb-4 text-sm text-panel-text">
          سيُحذف السجل <span className="text-panel-accent">{deleteRow ? String(deleteRow[cfg.pk]) : ''}</span> من {cfg.label} نهائياً، وسيُسجَّل في سجل التدقيق. هل أنت متأكد؟
        </p>
        <div className="flex gap-2">
          <Btn variant="danger" onClick={doDelete} disabled={busy}>{busy ? 'جارٍ…' : 'حذف نهائي'}</Btn>
          <Btn variant="ghost" onClick={() => setDeleteRow(null)}>إلغاء</Btn>
        </div>
      </Modal>
    </div>
  );
}

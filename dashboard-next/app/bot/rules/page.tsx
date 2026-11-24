'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Select, TextArea, Badge } from '@/components/ui';
import { OkText, ErrorText, ModeBadge } from '@/components/bot';

type Rule = {
  id: number;
  name: string;
  conditions: string;
  action: string;
  action_value: string | null;
  priority: number | null;
  hits: number | null;
  enabled: number;
  created_by: string | null;
  created_at: string;
};

const ACTIONS = ['accept', 'reject', 'review', 'alert', 'block_sender', 'send_to'];

export default function BotRules() {
  const { me } = useMe();
  const canEdit = me && ['operator', 'admin', 'super_admin'].includes(me.role);
  const { data, error, reload, loading } = useApi<{ rows: Rule[] }>('/api/db/rules?limit=200&order=priority', 30000);
  const [adding, setAdding] = React.useState(false);
  const [editing, setEditing] = React.useState<Rule | null>(null);
  const [confirmDel, setConfirmDel] = React.useState<Rule | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [form, setForm] = React.useState({ name: '', conditions: '', action: 'alert', action_value: '', priority: '100', enabled: '1' });

  const openAdd = () => {
    setForm({ name: '', conditions: '', action: 'alert', action_value: '', priority: '100', enabled: '1' });
    setAdding(true);
    setOkMsg(null);
    setErrMsg(null);
  };

  const openEdit = (r: Rule) => {
    setForm({
      name: r.name,
      conditions: r.conditions,
      action: r.action,
      action_value: r.action_value || '',
      priority: String(r.priority ?? 100),
      enabled: String(r.enabled ?? 1),
    });
    setEditing(r);
  };

  const save = async () => {
    setBusy(true);
    setOkMsg(null);
    setErrMsg(null);
    const body: Record<string, unknown> = {
      name: form.name,
      conditions: form.conditions,
      action: form.action,
    };
    if (form.action_value) body.action_value = form.action_value;
    body.priority = Number(form.priority);
    body.enabled = Number(form.enabled);
    const url = editing ? `/api/db/rules/${editing.id}` : '/api/db/rules';
    const r = await fetch(url, { method: editing ? 'PATCH' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) {
      setOkMsg(editing ? `حُفظت القاعدة #${editing.id} فعلياً` : 'أُنشئت القاعدة فعلياً في rules');
      setAdding(false);
      setEditing(null);
      reload();
    } else setErrMsg(`فشل: ${j.error || r.status}`);
  };

  const toggle = async (r: Rule) => {
    setBusy(true);
    const res = await fetch(`/api/db/rules/${r.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: r.enabled === 1 ? 0 : 1 }) });
    const j = (await res.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) reload(); else setErrMsg(`فشل التبديل: ${j.error}`);
  };

  const remove = async () => {
    if (!confirmDel) return;
    setBusy(true);
    const res = await fetch(`/api/db/rules/${confirmDel.id}`, { method: 'DELETE' });
    const j = (await res.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُذفت القاعدة #${confirmDel.id} فعلياً`); setConfirmDel(null); reload(); } else setErrMsg(`فشل: ${j.error}`);
  };

  const rows = data?.rows || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">قواعد المعالجة</h1>
        <div className="flex gap-2">
          <Gate role={me?.role} min="operator"><Btn onClick={openAdd}>إنشاء قاعدة</Btn></Gate>
          <Btn variant="ghost" onClick={() => reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title={`القواعد (${rows.length}) — CRUD حي مرتب بالأولوية`}>
        {loading && !data ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : rows.length === 0 ? (
          <div className="text-sm text-panel-muted">لا قواعد — أنشئ أول قاعدة</div>
        ) : (
          <div className="max-h-96 space-y-2 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            {rows.map((r) => (
              <div key={r.id} className="rounded-lg border border-panel-border bg-panel-bg p-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold">{r.name}</span>
                  <Badge tone="muted">أولوية {r.priority ?? '—'}</Badge>
                  <Badge tone="muted">{r.action}</Badge>
                  {r.enabled === 1 ? <Badge tone="ok">مفعّلة</Badge> : <Badge tone="warn">معطلة</Badge>}
                  {r.hits !== null && r.hits > 0 && <Badge tone="muted">تشغيلات: {r.hits}</Badge>}
                  <span className="text-panel-muted">{r.created_by || ''}</span>
                  <span className="ms-auto flex gap-1">
                    <Gate role={me?.role} min="operator">
                      <Btn variant="ghost" onClick={() => toggle(r)} disabled={busy}>{r.enabled === 1 ? 'تعطيل' : 'تفعيل'}</Btn>
                      <Btn variant="ghost" onClick={() => openEdit(r)}>تعديل</Btn>
                      <Btn variant="danger" onClick={() => setConfirmDel(r)}>حذف</Btn>
                    </Gate>
                  </span>
                </div>
                <div className="mt-1 break-all text-panel-muted" dir="ltr">{r.conditions}</div>
                {r.action_value && <div className="mt-1 text-panel-text" dir="ltr">→ {r.action_value}</div>}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title="القدرات">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ModeBadge mode="live" /> إنشاء/تعديل/حذف/تفعيل/أولوية — تنفيذ فعلي مدقّق
          <ModeBadge mode="contract" /> اختبار القاعدة على رسالة حقيقية — عقد §3.5
        </div>
        <div className="mt-2 text-[11px] text-panel-muted">الشروط تُخزن كما يفهمها المحرك (نص عقد الشروط) — التحقق من الشكل يجري هنا والتنفيذ الفعلي بواسطة محرك القواعد على Render</div>
      </Card>

      <Modal open={adding || editing !== null} title={editing ? `تعديل القاعدة #${editing.id}` : 'إنشاء قاعدة'} onClose={() => { setAdding(false); setEditing(null); }}>
        <div className="space-y-2">
          <Input placeholder="اسم القاعدة" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} maxLength={120} />
          <TextArea placeholder="الشروط (عقد المحرك — مثال: confidence>0.8 AND source_enabled AND not_duplicate)" value={form.conditions} onChange={(e) => setForm({ ...form, conditions: e.target.value })} rows={3} />
          <Select value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })}>
            {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
          </Select>
          <Input placeholder="قيمة الإجراء (وجهة/معرف — اختياري)" value={form.action_value} onChange={(e) => setForm({ ...form, action_value: e.target.value })} maxLength={2000} />
          <Input type="number" min={0} max={10000} placeholder="الأولوية" value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} />
          <Select value={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.value })}>
            <option value="1">مفعّلة</option>
            <option value="0">معطلة</option>
          </Select>
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => { setAdding(false); setEditing(null); }}>إلغاء</Btn>
            <Btn disabled={busy || !form.name || !form.conditions} onClick={save}>{busy ? 'جارٍ…' : 'حفظ فعلي'}</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={confirmDel !== null} title={`حذف القاعدة #${confirmDel?.id ?? ''}`} onClose={() => setConfirmDel(null)}>
        <p className="text-sm">حذف <b>{confirmDel?.name}</b> فعلياً — مسجل في التدقيق باسمك.</p>
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmDel(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy} onClick={remove}>حذف فعلي</Btn>
        </div>
      </Modal>
    </div>
  );
}

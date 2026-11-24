'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, TextArea, Badge } from '@/components/ui';
import { OkText, ErrorText, ModeBadge } from '@/components/bot';

type Setting = { key: string; value: string | null; updated_at: string };

export default function BotSettings() {
  const { me } = useMe();
  const canEdit = me && ['admin', 'super_admin'].includes(me.role);
  const { data, error, reload, loading } = useApi<{ rows: Setting[] }>('/api/db/app_settings?limit=200&order=key', 45000);
  const [search, setSearch] = React.useState('');
  const [editing, setEditing] = React.useState<Setting | null>(null);
  const [editValue, setEditValue] = React.useState('');
  const [adding, setAdding] = React.useState(false);
  const [newKey, setNewKey] = React.useState('');
  const [newValue, setNewValue] = React.useState('');
  const [confirmDel, setConfirmDel] = React.useState<Setting | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const rows = (data?.rows || []).filter((r) => !search || r.key.toLowerCase().includes(search.toLowerCase()));
  const masked = me?.role !== 'admin' && me?.role !== 'super_admin';

  const saveEdit = async () => {
    if (!editing) return;
    setBusy(true);
    const r = await fetch(`/api/db/app_settings/${encodeURIComponent(editing.key)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: editValue }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُفظ «${editing.key}» فعلياً (مسجل بالتدقيق قبل/بعد مموّه)`); setEditing(null); reload(); }
    else setErrMsg(`فشل الحفظ: ${j.error}`);
  };

  const addKey = async () => {
    setBusy(true);
    const r = await fetch('/api/db/app_settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key: newKey, value: newValue }) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`أُنشئ المفتاح «${newKey}» فعلياً`); setAdding(false); setNewKey(''); setNewValue(''); reload(); }
    else setErrMsg(`فشل: ${j.error}`);
  };

  const del = async () => {
    if (!confirmDel) return;
    setBusy(true);
    const r = await fetch(`/api/db/app_settings/${encodeURIComponent(confirmDel.key)}`, { method: 'DELETE' });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُذف «${confirmDel.key}» فعلياً`); setConfirmDel(null); reload(); } else setErrMsg(`فشل: ${j.error}`);
  };

  const isOpsKey = (k: string) => k === 'dashboard:ops';
  const opsValue = (v: string | null) => {
    try { return JSON.parse(v || '{}') as Record<string, unknown>; } catch { return {}; }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">إعدادات المحرك (KV حقيقي)</h1>
        <div className="flex gap-2">
          <Gate role={me?.role} min="admin"><Btn onClick={() => setAdding(true)}>مفتاح جديد</Btn></Gate>
          <Btn variant="ghost" onClick={() => reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title={`app_settings (${rows.length}) — القيم مموّهة لغير المدراء`}>
        <Input placeholder="بحث بالمفتاح" value={search} onChange={(e) => setSearch(e.target.value)} className="mb-3" />
        {loading && !data ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : rows.length === 0 ? (
          <div className="text-sm text-panel-muted">لا مفاتيح</div>
        ) : (
          <div className="max-h-96 space-y-2 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            {rows.map((s) => {
              const ops = isOpsKey(s.key) ? opsValue(s.value) : null;
              return (
                <div key={s.key} className="rounded-lg border border-panel-border bg-panel-bg p-3 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold" dir="ltr">{s.key}</span>
                    {ops && 'filtering_paused' in ops && (
                      <>
                        <Badge tone={ops.filtering_paused ? 'warn' : 'ok'}>المعالجة: {ops.filtering_paused ? 'موقوفة' : 'جارية'}</Badge>
                        <Badge tone={ops.alerts_paused ? 'bad' : 'ok'}>الإرسال: {ops.alerts_paused ? 'موقوف' : 'جارٍ'}</Badge>
                      </>
                    )}
                    <span className="text-panel-muted">{new Date(s.updated_at).toLocaleString('ar')}</span>
                    <span className="ms-auto flex gap-1">
                      <Gate role={me?.role} min="admin">
                        <Btn variant="ghost" onClick={() => { setEditing(s); setEditValue(String(s.value ?? '')); }}>تعديل</Btn>
                        <Btn variant="danger" onClick={() => setConfirmDel(s)}>حذف</Btn>
                      </Gate>
                    </span>
                  </div>
                  <div className="mt-1 max-h-24 overflow-y-auto break-all text-panel-muted" dir="ltr" style={{ scrollbarWidth: 'thin' }}>
                    {masked ? '•••••(مموّه عن دورك)' : s.value}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      <Card title="القدرات">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ModeBadge mode="live" /> قراءة/تعديل/إنشاء/حذف KV — تنفيذ فعلي مع تدقيق قبل/بعد مموّه
        </div>
        <div className="mt-2 text-[11px] text-panel-muted">مفاتيح النظام الحية: dashboard:ops (إيقاف المعالجة/الإرسال) · dashboard:keywords_json (سجل الكلمات) · boot:last · alert_cooldown · negmem:* · dashboard:filter_profile:* (ملفات التحليل). الأسرار لا تُعرض هنا إطلاقاً — أسرار الخدمات تعيش في env الخدمات.</div>
      </Card>

      <Modal open={editing !== null} title={`تعديل: ${editing?.key ?? ''}`} onClose={() => setEditing(null)}>
        <TextArea rows={8} value={editValue} onChange={(e) => setEditValue(e.target.value)} dir="ltr" />
        <div className="mt-2 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setEditing(null)}>إلغاء</Btn>
          <Btn disabled={busy} onClick={saveEdit}>{busy ? 'جارٍ…' : 'حفظ فعلي'}</Btn>
        </div>
      </Modal>

      <Modal open={adding} title="مفتاح KV جديد" onClose={() => setAdding(false)}>
        <div className="space-y-2">
          <Input placeholder="key (حروف/أرقام/:.-_)" value={newKey} onChange={(e) => setNewKey(e.target.value)} maxLength={100} dir="ltr" />
          <TextArea rows={5} placeholder="value" value={newValue} onChange={(e) => setNewValue(e.target.value)} dir="ltr" />
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setAdding(false)}>إلغاء</Btn>
            <Btn disabled={busy || !newKey} onClick={addKey}>إنشاء فعلي</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={confirmDel !== null} title={`حذف المفتاح: ${confirmDel?.key ?? ''}`} onClose={() => setConfirmDel(null)}>
        <p className="text-sm">حذف فعلي ومسجل في التدقيق — قد يؤثر على سلوك المحرك إن كان مفتاحاً حياً.</p>
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmDel(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy} onClick={del}>حذف فعلي</Btn>
        </div>
      </Modal>
    </div>
  );
}

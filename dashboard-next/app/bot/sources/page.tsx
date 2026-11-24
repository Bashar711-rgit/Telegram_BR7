'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Select, TextArea, Badge } from '@/components/ui';
import { OkText, ErrorText, ModeBadge } from '@/components/bot';

type Source = {
  id: number;
  chat_id: number;
  username: string | null;
  title: string | null;
  type: string;
  enabled: number;
  added_by: string | null;
  notes: string | null;
  chat_type: string | null;
  member_count: number | null;
  last_seen_at: string | null;
  created_at: string;
};

function parseCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = '';
  let q = false;
  for (const ch of line) {
    if (ch === '"') q = !q;
    else if (ch === ',' && !q) { out.push(cur); cur = ''; }
    else cur += ch;
  }
  out.push(cur);
  return out.map((s) => s.trim());
}

/** Extract chat id from a t.me link / @username / raw id (best-effort client hint; server validates). */
function parseSourceInput(raw: string): { chat_id: number; username: string | null; title: string | null; type: string } | null {
  const s = raw.trim();
  if (!s) return null;
  if (/^-?\d+$/.test(s)) return { chat_id: Number(s), username: null, title: null, type: 'unknown' };
  const m = s.match(/(?:t\.me\/|telegram\.me\/)([\w_]+)/i);
  const uname = m ? m[1] : s.startsWith('@') ? s.slice(1) : /[\w_]{3,64}/.test(s) ? s : null;
  if (!uname) return null;
  return { chat_id: 0, username: uname, title: null, type: 'unknown' };
}

export default function BotSources() {
  const { me } = useMe();
  const canEdit = me && ['operator', 'admin', 'super_admin'].includes(me.role);
  const { data, error, reload, loading } = useApi<{ rows: Source[] }>('/api/db/sources?limit=200&order=id&desc=1', 30000);
  const [search, setSearch] = React.useState('');
  const [adding, setAdding] = React.useState(false);
  const [bulk, setBulk] = React.useState(false);
  const [editing, setEditing] = React.useState<Source | null>(null);
  const [confirmDel, setConfirmDel] = React.useState<Source | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const [form, setForm] = React.useState({ chat_id: '', username: '', title: '', type: 'group', notes: '', chat_type: '' });
  const [bulkText, setBulkText] = React.useState('');
  const fileRef = React.useRef<HTMLInputElement>(null);

  const filtered = (data?.rows || []).filter((r) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return [String(r.chat_id), r.username, r.title, r.type].some((x) => (x || '').toLowerCase().includes(q));
  });

  const addOne = async (payload: Record<string, unknown>): Promise<boolean> => {
    const r = await fetch('/api/db/sources', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    if (!j.ok) setErrMsg((prev) => `${prev ? prev + ' · ' : ''}فشل إضافة ${String(payload.chat_id ?? payload.username)}: ${j.error}`);
    return j.ok;
  };

  const submitAdd = async () => {
    setBusy(true);
    setOkMsg(null);
    setErrMsg(null);
    const parsed = parseSourceInput(form.chat_id || form.username);
    const body: Record<string, unknown> = {
      chat_id: parsed && /^-?\d+$/.test(form.chat_id) ? Number(form.chat_id) : (parsed?.chat_id ?? 0),
      type: form.type || 'group',
    };
    if (form.username) body.username = form.username.replace(/^@/, '');
    if (form.title) body.title = form.title;
    if (form.notes) body.notes = form.notes;
    if (form.chat_type) body.chat_type = form.chat_type;
    const okAdd = await addOne(body);
    setBusy(false);
    if (okAdd) { setOkMsg('أُضيف المصدر فعلياً في sources'); setAdding(false); reload(); }
  };

  const submitBulk = async () => {
    setBusy(true);
    setOkMsg(null);
    setErrMsg(null);
    const lines = bulkText.split('\n').map((l) => l.trim()).filter(Boolean);
    let okc = 0;
    for (const line of lines) {
      const cols = parseCsvLine(line);
      const parsed = parseSourceInput(cols[0]);
      if (!parsed) { setErrMsg((p) => `${p ? p + ' · ' : ''}سطر غير مفهوم: ${line.slice(0, 40)}`); continue; }
      const body: Record<string, unknown> = {
        chat_id: /^-?\d+$/.test(cols[0]) ? Number(cols[0]) : parsed.chat_id,
        type: cols[1] || 'group',
      };
      if (parsed.username && !/^-?\d+$/.test(cols[0])) body.username = parsed.username;
      if (cols[2]) body.title = cols[2];
      if (cols[3]) body.notes = cols[3];
      if (await addOne(body)) okc++;
    }
    setBusy(false);
    if (okc > 0) { setOkMsg(`أُضيف ${okc}/${lines.length} مصدراً فعلياً`); setBulk(false); setBulkText(''); reload(); }
  };

  const toggle = async (s: Source) => {
    setBusy(true);
    const r = await fetch(`/api/db/sources/${s.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: s.enabled === 1 ? 0 : 1 }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) reload(); else setErrMsg(`فشل التبديل: ${j.error}`);
  };

  const saveEdit = async () => {
    if (!editing) return;
    setBusy(true);
    const body: Record<string, unknown> = {};
    if ((editing.username || '') !== form.username) body.username = form.username || null;
    if ((editing.title || '') !== form.title) body.title = form.title || null;
    if (editing.type !== form.type) body.type = form.type;
    if ((editing.notes || '') !== form.notes) body.notes = form.notes || null;
    const r = await fetch(`/api/db/sources/${editing.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg('حُفظ فعلياً'); setEditing(null); reload(); } else setErrMsg(`فشل: ${j.error}`);
  };

  const remove = async () => {
    if (!confirmDel) return;
    setBusy(true);
    const r = await fetch(`/api/db/sources/${confirmDel.id}`, { method: 'DELETE' });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُذف المصدر #${confirmDel.id} فعلياً`); setConfirmDel(null); reload(); } else setErrMsg(`فشل الحذف: ${j.error}`);
  };

  const exportCsv = () => {
    const rows = filtered;
    const csv = ['chat_id,type,username,title,notes,enabled', ...rows.map((r) => `${r.chat_id},${r.type},${r.username || ''},"${(r.title || '').replace(/"/g, '""')}",${r.notes || ''},${r.enabled}`)].join('\n');
    const url = URL.createObjectURL(new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' }));
    const a = document.createElement('a');
    a.href = url;
    a.download = `br7_sources_${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const importCsv = async (file: File) => {
    const text = await file.text();
    setBulkText(text.replace(/^\uFEFF/, ''));
    setBulk(true);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">إدارة مصادر المراقبة</h1>
        <div className="flex flex-wrap gap-2">
          <Gate role={me?.role} min="operator">
            <Btn onClick={() => { setForm({ chat_id: '', username: '', title: '', type: 'group', notes: '', chat_type: '' }); setAdding(true); }}>إضافة مصدر</Btn>
            <Btn variant="ghost" onClick={() => { setBulkText(''); setBulk(true); }}>إضافة متعددة / استيراد CSV</Btn>
          </Gate>
          <Btn variant="ghost" onClick={exportCsv}>تصدير CSV</Btn>
          <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden" onChange={(e) => e.target.files?.[0] && importCsv(e.target.files[0])} />
          <Btn variant="ghost" onClick={() => fileRef.current?.click()}>رفع ملف CSV</Btn>
          <Btn variant="ghost" onClick={() => reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title={`المصادر (${filtered.length}) — CRUD حي على sources`}>
        <Input placeholder="بحث: chat_id / username / عنوان / نوع" value={search} onChange={(e) => setSearch(e.target.value)} className="mb-3" />
        {loading && !data ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : filtered.length === 0 ? (
          <div className="text-sm text-panel-muted">لا مصادر مطابقة — أضف أول مصدر من الزر أعلاه</div>
        ) : (
          <div className="max-h-96 overflow-y-auto rounded-lg" style={{ scrollbarWidth: 'thin' }}>
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-panel-surface">
                <tr className="text-panel-muted">
                  <th className="p-2 text-right">#</th>
                  <th className="p-2 text-right">Chat ID</th>
                  <th className="p-2 text-right">المعرف</th>
                  <th className="p-2 text-right">العنوان</th>
                  <th className="p-2 text-right">النوع</th>
                  <th className="p-2 text-right">الحالة</th>
                  <th className="p-2 text-right">الأعضاء</th>
                  <th className="p-2 text-right">ملاحظات</th>
                  <th className="p-2"></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((s) => (
                  <tr key={s.id} className="border-t border-panel-border/40 hover:bg-panel-border/20">
                    <td className="p-2">{s.id}</td>
                    <td className="p-2" dir="ltr">{s.chat_id}</td>
                    <td className="p-2" dir="ltr">{s.username || '—'}</td>
                    <td className="p-2">{s.title || '—'}</td>
                    <td className="p-2">{s.type}</td>
                    <td className="p-2">{s.enabled === 1 ? <Badge tone="ok">مُراقب</Badge> : <Badge tone="warn">موقوف</Badge>}</td>
                    <td className="p-2">{s.member_count ?? '—'}</td>
                    <td className="p-2 max-w-32 truncate">{s.notes || '—'}</td>
                    <td className="p-2">
                      <Gate role={me?.role} min="operator">
                        <div className="flex gap-1">
                          <Btn variant="ghost" onClick={() => toggle(s)} disabled={busy}>{s.enabled === 1 ? 'إيقاف' : 'تفعيل'}</Btn>
                          <Btn variant="ghost" onClick={() => { setEditing(s); setForm({ chat_id: String(s.chat_id), username: s.username || '', title: s.title || '', type: s.type, notes: s.notes || '', chat_type: s.chat_type || '' }); }}>تعديل</Btn>
                          <Btn variant="danger" onClick={() => setConfirmDel(s)}>حذف</Btn>
                        </div>
                      </Gate>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="القدرات">
        <div className="flex flex-wrap gap-2 text-xs">
          <ModeBadge mode="live" /> إضافة/تعديل/حذف/تفعيل/متعددة/استيراد وتصدير CSV — كلها تنفذ فعلاً
          <ModeBadge mode="contract" /> فحص وصول/إعادة اتصال — عقد §3.4 (نقطة المحرك)
        </div>
        <div className="mt-2 text-[11px] text-panel-muted">تنسيق الاستيراد: chat_id,type,username,title,notes — أو رابط t.me أو @username في العمود الأول (النوع يبقى editable لاحقاً)</div>
      </Card>

      <Modal open={adding} title="إضافة مصدر" onClose={() => setAdding(false)}>
        <div className="space-y-2">
          <Input placeholder="Chat ID أو رابط t.me أو @username" value={form.chat_id || form.username} onChange={(e) => {
            const v = e.target.value;
            if (/^-?\d+$/.test(v)) setForm({ ...form, chat_id: v, username: '' });
            else setForm({ ...form, username: v, chat_id: '' });
          }} />
          <Select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
            <option value="group">مجموعة</option>
            <option value="channel">قناة</option>
            <option value="unknown">غير محدد</option>
          </Select>
          <Input placeholder="العنوان (اختياري)" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} maxLength={120} />
          <Input placeholder="chat_type (اختياري)" value={form.chat_type} onChange={(e) => setForm({ ...form, chat_type: e.target.value })} maxLength={20} />
          <TextArea placeholder="ملاحظات (اختياري)" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} maxLength={500} rows={2} />
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setAdding(false)}>إلغاء</Btn>
            <Btn disabled={busy || (!form.chat_id && !form.username)} onClick={submitAdd}>{busy ? 'جارٍ…' : 'إضافة فعلياً'}</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={bulk} title="إضافة متعددة / استيراد CSV" onClose={() => setBulk(false)}>
        <TextArea rows={8} value={bulkText} onChange={(e) => setBulkText(e.target.value)} placeholder={'chat_id,type,username,title,notes\n-1001234567890,group,,مجموعة التقنية,\n@some_channel,channel,,قناة,مراقبة عالية'} />
        <div className="mt-2 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setBulk(false)}>إلغاء</Btn>
          <Btn disabled={busy || !bulkText.trim()} onClick={submitBulk}>{busy ? 'جارٍ…' : 'إضافة الكل فعلياً'}</Btn>
        </div>
      </Modal>

      <Modal open={editing !== null} title={`تعديل المصدر #${editing?.id ?? ''}`} onClose={() => setEditing(null)}>
        <div className="space-y-2">
          <Input placeholder="المعرف" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} maxLength={64} />
          <Input placeholder="العنوان" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} maxLength={120} />
          <Select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
            <option value="group">مجموعة</option>
            <option value="channel">قناة</option>
            <option value="unknown">غير محدد</option>
          </Select>
          <TextArea placeholder="ملاحظات" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} maxLength={500} rows={2} />
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setEditing(null)}>إلغاء</Btn>
            <Btn disabled={busy} onClick={saveEdit}>{busy ? 'جارٍ…' : 'حفظ فعلي'}</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={confirmDel !== null} title={`حذف المصدر #${confirmDel?.id ?? ''}`} onClose={() => setConfirmDel(null)}>
        <p className="text-sm">سيُحذف <b dir="ltr">{confirmDel?.username || confirmDel?.chat_id}</b> من المراقبة فعلياً (مسجل في التدقيق باسمك).</p>
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmDel(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy} onClick={remove}>حذف فعلي</Btn>
        </div>
      </Modal>
    </div>
  );
}

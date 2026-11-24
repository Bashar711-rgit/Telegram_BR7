'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Select, Badge } from '@/components/ui';
import { OkText, ErrorText, ModeBadge, ContractNote } from '@/components/bot';

type Blocked = { sender_id: number; reason: string; blocked_by: string | null; blocked_at: string };
type Allowed = { id: number; entity_type: string; entity_id: number; note: string | null; created_at: string };

export default function BotBlocked() {
  const { me } = useMe();
  const canEdit = me && ['operator', 'admin', 'super_admin'].includes(me.role);
  const { data: bData, error: bErr, reload: reloadB, loading: bLoading } = useApi<{ rows: Blocked[] }>('/api/db/blocked_senders?limit=200&order=blocked_at&desc=1', 45000);
  const { data: aData, error: aErr, reload: reloadA, loading: aLoading } = useApi<{ rows: Allowed[] }>('/api/db/allowed_entities?limit=200&order=id&desc=1', 45000);

  const [addB, setAddB] = React.useState(false);
  const [addA, setAddA] = React.useState(false);
  const [formB, setFormB] = React.useState({ sender_id: '', reason: '' });
  const [formA, setFormA] = React.useState({ entity_type: 'user', entity_id: '', note: '' });
  const [confirmDelB, setConfirmDelB] = React.useState<Blocked | null>(null);
  const [confirmDelA, setConfirmDelA] = React.useState<Allowed | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const addBlocked = async () => {
    setBusy(true);
    const r = await fetch('/api/db/blocked_senders', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ sender_id: Number(formB.sender_id), reason: formB.reason }) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُظر ${formB.sender_id} فعلياً — الحظر موحد عبر قاعدة البيانات لكل حسابات المحرك`); setAddB(false); setFormB({ sender_id: '', reason: '' }); reloadB(); }
    else setErrMsg(`فشل الحظر: ${j.error}`);
  };

  const addAllowed = async () => {
    setBusy(true);
    const body: Record<string, unknown> = { entity_type: formA.entity_type, entity_id: Number(formA.entity_id) };
    if (formA.note) body.note = formA.note;
    const r = await fetch('/api/db/allowed_entities', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg('أُضيف للموثوقين فعلياً'); setAddA(false); setFormA({ entity_type: 'user', entity_id: '', note: '' }); reloadA(); }
    else setErrMsg(`فشل: ${j.error}`);
  };

  const delBlocked = async () => {
    if (!confirmDelB) return;
    setBusy(true);
    const r = await fetch(`/api/db/blocked_senders/${confirmDelB.sender_id}`, { method: 'DELETE' });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`أُلغي حظر ${confirmDelB.sender_id} فعلياً`); setConfirmDelB(null); reloadB(); } else setErrMsg(`فشل: ${j.error}`);
  };

  const delAllowed = async () => {
    if (!confirmDelA) return;
    setBusy(true);
    const r = await fetch(`/api/db/allowed_entities/${confirmDelA.id}`, { method: 'DELETE' });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`أُزيل العنصر #${confirmDelA.id} فعلياً`); setConfirmDelA(null); reloadA(); } else setErrMsg(`فشل: ${j.error}`);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">الحظر والقوائم الموثوقة</h1>
        <div className="flex gap-2">
          <Gate role={me?.role} min="operator">
            <Btn onClick={() => setAddB(true)}>حظر مرسل</Btn>
            <Btn variant="ghost" onClick={() => setAddA(true)}>إضافة موثوق</Btn>
          </Gate>
          <Btn variant="ghost" onClick={() => { reloadB(); reloadA(); }}>تحديث</Btn>
        </div>
      </div>
      {(bErr || aErr) && <div className="text-sm text-panel-bad">خطأ: {bErr || aErr}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title={`المحظورون (${bData?.rows.length ?? 0}) — حظر موحد عبر المحرك`}>
        {bLoading && !bData ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : (bData?.rows.length ?? 0) === 0 ? (
          <div className="text-sm text-panel-muted">لا محظورين</div>
        ) : (
          <div className="max-h-80 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-panel-surface"><tr className="text-panel-muted">
                <th className="p-2 text-right">Sender ID</th><th className="p-2 text-right">السبب</th><th className="p-2 text-right">بواسطة</th><th className="p-2 text-right">التاريخ</th><th className="p-2"></th>
              </tr></thead>
              <tbody>
                {bData!.rows.map((b) => (
                  <tr key={b.sender_id} className="border-t border-panel-border/40 hover:bg-panel-border/20">
                    <td className="p-2" dir="ltr">{b.sender_id}</td>
                    <td className="p-2 max-w-60 truncate">{b.reason}</td>
                    <td className="p-2">{b.blocked_by || '—'}</td>
                    <td className="p-2">{new Date(b.blocked_at).toLocaleString('ar')}</td>
                    <td className="p-2"><Gate role={me?.role} min="operator"><Btn variant="danger" onClick={() => setConfirmDelB(b)}>إلغاء الحظر</Btn></Gate></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title={`الموثوقون (${aData?.rows.length ?? 0}) — استثناءات التصفية`}>
        {aLoading && !aData ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : (aData?.rows.length ?? 0) === 0 ? (
          <div className="text-sm text-panel-muted">لا عناصر موثوقة</div>
        ) : (
          <div className="max-h-80 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-panel-surface"><tr className="text-panel-muted">
                <th className="p-2 text-right">#</th><th className="p-2 text-right">النوع</th><th className="p-2 text-right">المعرف</th><th className="p-2 text-right">ملاحظة</th><th className="p-2 text-right">التاريخ</th><th className="p-2"></th>
              </tr></thead>
              <tbody>
                {aData!.rows.map((a) => (
                  <tr key={a.id} className="border-t border-panel-border/40 hover:bg-panel-border/20">
                    <td className="p-2">{a.id}</td>
                    <td className="p-2"><Badge tone="muted">{a.entity_type}</Badge></td>
                    <td className="p-2" dir="ltr">{a.entity_id}</td>
                    <td className="p-2 max-w-60 truncate">{a.note || '—'}</td>
                    <td className="p-2">{new Date(a.created_at).toLocaleString('ar')}</td>
                    <td className="p-2"><Gate role={me?.role} min="operator"><Btn variant="danger" onClick={() => setConfirmDelA(a)}>إزالة</Btn></Gate></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="القدرات">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ModeBadge mode="live" /> حظر/إلغاء + موثوقون (تنفيذ فعلي موحد عبر DB)
          <ModeBadge mode="contract" /> blocked_chats (منح قراءة غير مضبوط — عقد §1.1) · حظر مؤقت بمدة (عمود expires_at — §4) · سجل تاريخ الحظر الكامل في /bot/audit
        </div>
        <ContractNote>الحظر عبر blocked_senders يعمل موحداً على مستوى كل حسابات المحرك لأن الجميع يقرأ نفس جدول قاعدة البيانات — لا توزيع محلي.</ContractNote>
      </Card>

      <Modal open={addB} title="حظر مرسل" onClose={() => setAddB(false)}>
        <div className="space-y-2">
          <Input placeholder="Sender ID الرقمي" type="number" value={formB.sender_id} onChange={(e) => setFormB({ ...formB, sender_id: e.target.value })} />
          <Select value={formB.reason === '' ? 'اختر سبباً أو اكتب' : formB.reason} onChange={(e) => setFormB({ ...formB, reason: e.target.value })}>
            <option disabled value="اختر سبباً أو اكتب">اختر سبباً أو اكتب</option>
            <option value="سبام">سبام</option>
            <option value="إعلانات">إعلانات</option>
            <option value="طلب غير حقيقي">طلب غير حقيقي</option>
            <option value="إساءة">إساءة</option>
          </Select>
          <Input placeholder="سبب مخصص" value={formB.reason === 'اختر سبباً أو اكتب' ? '' : formB.reason} onChange={(e) => setFormB({ ...formB, reason: e.target.value })} maxLength={300} />
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setAddB(false)}>إلغاء</Btn>
            <Btn disabled={busy || !formB.sender_id || !formB.reason} onClick={addBlocked}>حظر فعلي</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={addA} title="إضافة عنصر موثوق" onClose={() => setAddA(false)}>
        <div className="space-y-2">
          <Select value={formA.entity_type} onChange={(e) => setFormA({ ...formA, entity_type: e.target.value })}>
            <option value="user">مستخدم</option>
            <option value="group">مجموعة</option>
            <option value="channel">قناة</option>
          </Select>
          <Input placeholder="Telegram ID" type="number" value={formA.entity_id} onChange={(e) => setFormA({ ...formA, entity_id: e.target.value })} />
          <Input placeholder="ملاحظة (اختياري)" value={formA.note} onChange={(e) => setFormA({ ...formA, note: e.target.value })} maxLength={500} />
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setAddA(false)}>إلغاء</Btn>
            <Btn disabled={busy || !formA.entity_id} onClick={addAllowed}>إضافة فعلياً</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={confirmDelB !== null} title={`إلغاء حظر ${confirmDelB?.sender_id ?? ''}`} onClose={() => setConfirmDelB(null)}>
        <p className="text-sm">سيُلغى الحظر فعلياً (مسجل في التدقيق).</p>
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmDelB(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy} onClick={delBlocked}>إلغاء الحظر فعلياً</Btn>
        </div>
      </Modal>

      <Modal open={confirmDelA !== null} title={`إزالة العنصر #${confirmDelA?.id ?? ''}`} onClose={() => setConfirmDelA(null)}>
        <p className="text-sm">سيُزال من الموثوقين فعلياً.</p>
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmDelA(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy} onClick={delAllowed}>إزالة فعلياً</Btn>
        </div>
      </Modal>
    </div>
  );
}

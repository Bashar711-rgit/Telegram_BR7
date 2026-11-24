'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Select, Badge } from '@/components/ui';
import { ModeBadge, ContractNote, OkText, ErrorText, Row } from '@/components/bot';

type Account = {
  id: number;
  phone: string;
  display_name: string | null;
  username: string | null;
  tg_user_id: number | null;
  status: string | null;
  monitoring_enabled: number;
  priority: number | null;
  is_main: number;
  last_error: string | null;
  last_error_at: string | null;
  created_at: string;
  updated_at: string;
};

/** Add-account wizard contract steps (§3.3) — UI prepared, executor on Render pending. */
const WIZARD_STEPS = [
  { n: 1, t: 'إدخال رقم الهاتف', d: 'POST /admin/api/accounts/link_start {phone} → flow_id + حالة waiting_code' },
  { n: 2, t: 'إرسال رمز التحقق (OTP)', d: 'تلقائي من Telegram إلى الحساب — لا يُخزن أبداً' },
  { n: 3, t: 'إدخال OTP', d: 'POST /admin/api/accounts/link_code {flow_id, code}' },
  { n: 4, t: 'كلمة مرور 2FA (إن طُلبت)', d: 'POST /admin/api/accounts/link_2fa {flow_id, password} — تعيش في الذاكرة حصراً' },
  { n: 5, t: 'إنشاء الجلسة وتشفيرها', d: 'Session String مشفرة Fernet في session_encrypted — لا تعود للواجهة' },
  { n: 6, t: 'اختبار الاتصال والتأكيد', d: 'POST /admin/api/accounts/{id}/test_connection' },
];

export default function BotAccounts() {
  const { me } = useMe();
  const canEdit = me && ['admin', 'super_admin'].includes(me.role);
  const { data, error, reload, loading } = useApi<{ table: string; pk: string; rows: Account[] }>(
    '/api/db/panel_accounts?limit=100',
    30000,
  );
  const [detail, setDetail] = React.useState<Account | null>(null);
  const [editing, setEditing] = React.useState<Account | null>(null);
  const [wizard, setWizard] = React.useState(false);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [form, setForm] = React.useState<{ display_name: string; priority: string; monitoring_enabled: string; status: string; is_main: string }>({
    display_name: '', priority: '0', monitoring_enabled: '1', status: '', is_main: '0',
  });

  const openEdit = (a: Account) => {
    setForm({
      display_name: a.display_name || '',
      priority: String(a.priority ?? 0),
      monitoring_enabled: String(a.monitoring_enabled ?? 1),
      status: a.status || '',
      is_main: String(a.is_main ?? 0),
    });
    setEditing(a);
    setOkMsg(null);
    setErrMsg(null);
  };

  const saveEdit = async () => {
    if (!editing) return;
    setBusy(true);
    const body: Record<string, unknown> = {};
    if (form.display_name !== (editing.display_name || '')) body.display_name = form.display_name || null;
    if (Number(form.priority) !== (editing.priority ?? 0)) body.priority = Number(form.priority);
    if (Number(form.monitoring_enabled) !== (editing.monitoring_enabled ?? 1)) body.monitoring_enabled = Number(form.monitoring_enabled);
    if (form.status !== (editing.status || '')) body.status = form.status || null;
    if (Number(form.is_main) !== (editing.is_main ?? 0)) body.is_main = Number(form.is_main);
    const r = await fetch(`/api/db/panel_accounts/${editing.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) {
      setOkMsg('حُفظ فعلياً في panel_accounts — المحرك يقرأ التغييرات وفق مزامنته الحية');
      setEditing(null);
      reload();
    } else setErrMsg(`فشل الحفظ: ${j.error || r.status}`);
  };

  const rows = data?.rows || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">إدارة حسابات Telegram</h1>
        <div className="flex gap-2">
          <Btn variant="ghost" onClick={() => setWizard(true)}>معالج إضافة حساب</Btn>
          <Btn onClick={() => reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title={`الحسابات التشغيلية (${rows.length}) — قراءة حية من panel_accounts`}>
        {loading && !data ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : rows.length === 0 ? (
          <div className="text-sm text-panel-muted">لا حسابات — استخدم معالج الإضافة بعد توفر منفذ Render (عقد §3.3)</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-panel-muted">
                  <th className="p-2 text-right">#</th>
                  <th className="p-2 text-right">الهاتف</th>
                  <th className="p-2 text-right">الاسم</th>
                  <th className="p-2 text-right">المعرف</th>
                  <th className="p-2 text-right">الحالة</th>
                  <th className="p-2 text-right">المراقبة</th>
                  <th className="p-2 text-right">الأولوية</th>
                  <th className="p-2 text-right">رئيسي</th>
                  <th className="p-2 text-right">آخر خطأ</th>
                  <th className="p-2"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id} className="border-t border-panel-border/40 hover:bg-panel-border/20">
                    <td className="p-2">{a.id}</td>
                    <td className="p-2 font-bold">{a.phone}</td>
                    <td className="p-2">{a.display_name || '—'}</td>
                    <td className="p-2">{a.username || a.tg_user_id || '—'}</td>
                    <td className="p-2">
                      <Badge tone={a.status === 'active' || a.status === 'ready' ? 'ok' : a.last_error ? 'bad' : 'muted'}>{a.status || '—'}</Badge>
                    </td>
                    <td className="p-2">{a.monitoring_enabled === 1 ? <Badge tone="ok">مفعّل</Badge> : <Badge tone="warn">موقوف</Badge>}</td>
                    <td className="p-2">{a.priority ?? '—'}</td>
                    <td className="p-2">{a.is_main === 1 ? <Badge tone="ok">رئيسي</Badge> : ''}</td>
                    <td className="p-2 max-w-40 truncate text-panel-bad">{a.last_error || '—'}</td>
                    <td className="p-2">
                      <div className="flex gap-1">
                        <Btn variant="ghost" onClick={() => setDetail(a)}>تفاصيل</Btn>
                        <Gate role={me?.role} min="admin"><Btn variant="ghost" onClick={() => openEdit(a)}>تعديل</Btn></Gate>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="قدرات الحسابات">
        <div className="space-y-2 text-xs">
          <div className="flex items-center gap-2"><ModeBadge mode="live" /> قراءة التفاصيل الكاملة + تعديل الاسم/الأولوية/المراقبة/الحالة/الرئيسي</div>
          <div className="flex items-center gap-2"><ModeBadge mode="contract" /> إضافة حساب (هاتف→OTP→2FA→جلسة مشفرة) — عقد §3.3</div>
          <div className="flex items-center gap-2"><ModeBadge mode="contract" /> حذف/إعادة مصادقة/اختبار اتصال — عقد §3.3</div>
        </div>
        <ContractNote>
          حماية الجلسات: session_encrypted/api_hash_encrypted غير قابلة للقراءة من اللوحة (سماحية أعمدة على الخادم) — لا Session String يظهر أبداً.
        </ContractNote>
      </Card>

      <Modal open={detail !== null} title={`تفاصيل الحساب #${detail?.id ?? ''}`} onClose={() => setDetail(null)}>
        {detail && (
          <div className="space-y-1">
            <Row k="Telegram ID">{detail.tg_user_id ?? '—'}</Row>
            <Row k="الهاتف">{detail.phone}</Row>
            <Row k="Username">{detail.username || '—'}</Row>
            <Row k="الاسم المعروض">{detail.display_name || '—'}</Row>
            <Row k="الحالة">{detail.status || '—'}</Row>
            <Row k="الجلسة">مشفرة — غير معروضة</Row>
            <Row k="المراقبة">{detail.monitoring_enabled === 1 ? 'مفعّلة' : 'موقوفة'}</Row>
            <Row k="الأولوية">{detail.priority ?? '—'}</Row>
            <Row k="آخر خطأ">{detail.last_error || '—'} {detail.last_error_at ? `(${new Date(detail.last_error_at).toLocaleString('ar')})` : ''}</Row>
            <Row k="أُنشئ">{new Date(detail.created_at).toLocaleString('ar')}</Row>
            <Row k="آخر تحديث">{new Date(detail.updated_at).toLocaleString('ar')}</Row>
            <ContractNote>الرسائل المستلمة/المعالجة/التنبيهات لكل حساب تتطلب منح sender_stats (عقد §3.7) — غير متاح في هذه المرحلة بصدق.</ContractNote>
          </div>
        )}
      </Modal>

      <Modal open={editing !== null} title={`تعديل الحساب #${editing?.id ?? ''}`} onClose={() => setEditing(null)}>
        <div className="space-y-2">
          <label className="block text-xs text-panel-muted">الاسم المعروض
            <Input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} maxLength={64} />
          </label>
          <label className="block text-xs text-panel-muted">الحالة
            <Input value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value })} placeholder="active | disabled | ..." maxLength={30} />
          </label>
          <label className="block text-xs text-panel-muted">الأولوية
            <Input type="number" min={0} max={100} value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })} />
          </label>
          <label className="block text-xs text-panel-muted">المراقبة
            <Select value={form.monitoring_enabled} onChange={(e) => setForm({ ...form, monitoring_enabled: e.target.value })}>
              <option value="1">مفعّلة</option>
              <option value="0">موقوفة</option>
            </Select>
          </label>
          <label className="block text-xs text-panel-muted">الحساب الرئيسي
            <Select value={form.is_main} onChange={(e) => setForm({ ...form, is_main: e.target.value })}>
              <option value="0">لا</option>
              <option value="1">نعم</option>
            </Select>
          </label>
          <div className="flex justify-end gap-2 pt-2">
            <Btn variant="ghost" onClick={() => setEditing(null)}>إلغاء</Btn>
            <Btn disabled={busy} onClick={saveEdit}>{busy ? 'جارٍ الحفظ…' : 'حفظ فعلي'}</Btn>
          </div>
        </div>
      </Modal>

      <Modal open={wizard} title="معالج إضافة حساب — واجهة جاهزة والمنفذ ينتظر" onClose={() => setWizard(false)}>
        <div className="space-y-2">
          {WIZARD_STEPS.map((s) => (
            <div key={s.n} className="rounded-lg border border-panel-border bg-panel-bg p-3">
              <div className="flex items-center gap-2 text-sm font-bold">
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-panel-accent/15 text-panel-accent">{s.n}</span>
                {s.t}
              </div>
              <div className="mt-1 ps-7 text-xs text-panel-muted" dir="ltr">{s.d}</div>
            </div>
          ))}
          <ContractNote>
            التنفيذ يتطلب تدفق Telethon تفاعلي على خدمة Render (عقد §3.3 موثق في BOT_CONTROL_CONTRACTS.md) — الأزرار هنا لا تُرجع أي نجاح وهمي، وسيتصل المعالج فور توفير المنفذ دون إعادة بناء الواجهة.
          </ContractNote>
        </div>
      </Modal>
    </div>
  );
}

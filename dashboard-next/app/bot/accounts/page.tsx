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

type LinkOp = {
  op_id: string;
  state: string;
  error_code?: string | null;
  error_message?: string | null;
  flood_wait?: number;
  attempts_left_code?: number;
  attempts_left_password?: number;
  phone?: string;
  me?: { id: number; name: string; username?: string | null; phone: string } | null;
  account_row_id?: number | null;
  age_seconds?: number;
};

type WStep = 'phone' | 'code' | 'password' | 'done';

/** ربط الحساب يدخل المحرك مباشرة عند نجاح المصادقة — لقطة ناجحة تعود من link_manager. */
function terminalState(s?: string | null): boolean {
  return s === 'ready' || s === 'active';
}

export default function BotAccounts() {
  const { me } = useMe();
  const canEdit = me && ['admin', 'super_admin'].includes(me.role);
  const { data, error, reload, loading } = useApi<{ table: string; pk: string; rows: Account[] }>(
    '/api/db/panel_accounts?limit=100',
    30000,
  );
  const [detail, setDetail] = React.useState<Account | null>(null);
  const [editing, setEditing] = React.useState<Account | null>(null);
  const [deleting, setDeleting] = React.useState<Account | null>(null);
  const [wizard, setWizard] = React.useState(false);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [form, setForm] = React.useState<{ display_name: string; priority: string; monitoring_enabled: string; status: string; is_main: string }>({
    display_name: '', priority: '0', monitoring_enabled: '1', status: '', is_main: '0',
  });

  // ── حالة معالج الإضافة الحقيقي (تدفق Telethon على المحرك عبر بوابة الخدمة) ──
  const [wStep, setWStep] = React.useState<WStep>('phone');
  const [wPhone, setWPhone] = React.useState('');
  const [wOpId, setWOpId] = React.useState<string | null>(null);
  const [wCode, setWCode] = React.useState('');
  const [wPassword, setWPassword] = React.useState('');
  const [wBusy, setWBusy] = React.useState(false);
  const [wErr, setWErr] = React.useState<string | null>(null);
  const [wInfo, setWInfo] = React.useState<string | null>(null);
  const [wMe, setWMe] = React.useState<{ id: number; name: string; username?: string | null; phone: string } | null>(null);
  const [wRowId, setWRowId] = React.useState<number | null>(null);

  const resetWizard = () => {
    setWStep('phone');
    setWPhone('');
    setWOpId(null);
    setWCode('');
    setWPassword('');
    setWBusy(false);
    setWErr(null);
    setWInfo(null);
    setWMe(null);
    setWRowId(null);
  };

  const openWizard = () => {
    resetWizard();
    setWizard(true);
  };

  const callLink = async (body: Record<string, unknown>) => {
    const r = await fetch('/api/bot/accounts/link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return (await r.json()) as { ok: boolean; data?: { op?: LinkOp | null; cancelled?: boolean | null }; error?: string };
  };

  /** بعد انقطاع الاستجابة (مهلة Vercel أثناء التفعيل) — نستعلم الحقيقة من المحرك بدل افتراض فشل. */
  const pollStatus = async (opId: string): Promise<LinkOp | null> => {
    const r = await fetch(`/api/bot/accounts/link?op_id=${encodeURIComponent(opId)}`);
    const j = (await r.json()) as { ok: boolean; data?: { op?: LinkOp | null }; error?: string };
    return j.ok ? j.data?.op || null : null;
  };

  const startLink = async () => {
    setWBusy(true);
    setWErr(null);
    setWInfo(null);
    const j = await callLink({ action: 'start', phone: wPhone.trim() });
    setWBusy(false);
    if (j.ok && j.data?.op) {
      if (j.data.op.state === 'waiting_code') {
        setWOpId(j.data.op.op_id);
        setWStep('code');
        setWInfo(`أُرسل رمز التأكيد إلى ${j.data.op.phone || wPhone} عبر تيليجرام — TTL العملية 15 دقيقة`);
      } else {
        setWErr(j.data.op.error_message || j.data.op.error_code || 'فشل غير محدد');
      }
    } else {
      setWErr(j.error || 'فشل غير محدد');
    }
  };

  const finishLink = (op: LinkOp | null) => {
    if (op && terminalState(op.state)) {
      setWMe(op.me || null);
      setWRowId(op.account_row_id ?? null);
      setWStep('done');
      reload();
      return true;
    }
    return false;
  };

  const submitCode = async () => {
    if (!wOpId) return;
    setWBusy(true);
    setWErr(null);
    setWInfo('جارٍ التحقق وإنشاء الجلسة المشفرة… قد يستغرق حتى دقيقة');
    let j = await callLink({ action: 'code', op_id: wOpId, code: wCode.trim() });
    let op = j.ok ? j.data?.op || null : null;
    // مهلة الوسيط أثناء تفعيل المراقبة لا تعني فشل المصادقة — الحقيقة من المحرك
    if (!j.ok && (j.error || '').includes('engine_unreachable') && wOpId) {
      op = await pollStatus(wOpId);
    }
    setWBusy(false);
    setWInfo(null);
    if (op && finishLink(op)) return;
    if (op?.state === 'waiting_password') {
      setWStep('password');
      setWInfo('الحساب محمي بكلمة مرور 2FA — أدخلها لإكمال الربط');
      return;
    }
    if (op?.state === 'waiting_code') {
      setWErr(`الرمز غير صحيح — المحاولات المتبقية: ${op.attempts_left_code ?? 0}`);
      return;
    }
    setWErr(j.error || op?.error_message || op?.error_code || 'فشل غير محدد');
  };

  const submitPassword = async () => {
    if (!wOpId) return;
    setWBusy(true);
    setWErr(null);
    setWInfo('جارٍ التحقق وإنشاء الجلسة المشفرة… قد يستغرق حتى دقيقة');
    let j = await callLink({ action: 'password', op_id: wOpId, password: wPassword });
    let op = j.ok ? j.data?.op || null : null;
    if (!j.ok && (j.error || '').includes('engine_unreachable') && wOpId) {
      op = await pollStatus(wOpId);
    }
    setWBusy(false);
    setWInfo(null);
    if (op && finishLink(op)) return;
    if (op?.state === 'waiting_password') {
      setWErr(`كلمة المرور غير صحيحة — المحاولات المتبقية: ${op.attempts_left_password ?? 0}`);
      return;
    }
    setWErr(j.error || op?.error_message || op?.error_code || 'فشل غير محدد');
  };

  const cancelWizard = async () => {
    if (wOpId) {
      await callLink({ action: 'cancel', op_id: wOpId });
    }
    setWizard(false);
    resetWizard();
  };

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
    setErrMsg(null);
    // المراقبة تُطبَّق فوراً على المحرك (spawn/stop) عبر بوابة الخدمة — ليست كتابة DB صامتة
    const monChanged = Number(form.monitoring_enabled) !== (editing.monitoring_enabled ?? 1);
    const body: Record<string, unknown> = {};
    if (form.display_name !== (editing.display_name || '')) body.display_name = form.display_name || null;
    if (Number(form.priority) !== (editing.priority ?? 0)) body.priority = Number(form.priority);
    if (form.status !== (editing.status || '')) body.status = form.status || null;
    if (Number(form.is_main) !== (editing.is_main ?? 0)) body.is_main = Number(form.is_main);
    let patchOk = true;
    if (Object.keys(body).length) {
      const r = await fetch(`/api/db/panel_accounts/${editing.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const j = (await r.json()) as { ok: boolean; error?: string };
      patchOk = j.ok;
      if (!j.ok) setErrMsg(`فشل حفظ البيانات: ${j.error || r.status}`);
    }
    let toggleOk = true;
    if (monChanged) {
      const r = await fetch('/api/bot/accounts/row', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ row_id: editing.id, action: 'toggle', enable: Number(form.monitoring_enabled) === 1 }),
      });
      const j = (await r.json()) as { ok: boolean; error?: string };
      toggleOk = j.ok;
      if (!j.ok) setErrMsg((m) => `${m ? m + ' — ' : ''}فشل تطبيق المراقبة على المحرك: ${j.error || r.status}`);
    }
    setBusy(false);
    if (patchOk && toggleOk) {
      setOkMsg(
        monChanged
          ? 'حُفظ فعلياً وطُبّقت المراقبة فوراً على المحرك (spawn/stop)'
          : 'حُفظ فعلياً في panel_accounts',
      );
      setEditing(null);
      reload();
    }
  };

  const deleteRow = async () => {
    if (!deleting) return;
    setBusy(true);
    const r = await fetch('/api/bot/accounts/row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ row_id: deleting.id, action: 'delete' }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string; data?: { monitorStop?: string } };
    setBusy(false);
    if (j.ok) {
      const stop = j.data?.monitorStop;
      setOkMsg(
        stop === 'deferred_engine_asleep'
          ? `حُذف الحساب #${deleting.id} فعلياً من panel_accounts — المحرك نائم حالياً فيتوقف مراقبه تلقائياً عند استيقاظه`
          : `حُذف الحساب #${deleting.id} فعلياً من panel_accounts وأُوقفت مراقبته على المحرك`,
      );
      setDeleting(null);
      reload();
    } else {
      setErrMsg(`فشل الحذف: ${j.error || r.status}`);
      setDeleting(null);
    }
  };

  const rows = data?.rows || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">إدارة حسابات Telegram</h1>
        <div className="flex gap-2">
          <Gate role={me?.role} min="admin"><Btn onClick={openWizard}>إضافة حساب</Btn></Gate>
          <Btn variant="ghost" onClick={() => reload()}>تحديث</Btn>
        </div>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title={`الحسابات التشغيلية (${rows.length}) — قراءة حية من panel_accounts`}>
        {loading && !data ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : rows.length === 0 ? (
          <div className="text-sm text-panel-muted">لا حسابات — افتح «إضافة حساب» لربط أول حساب عبر تيليجرام</div>
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
                        <Gate role={me?.role} min="super_admin"><Btn variant="ghost" onClick={() => setDeleting(a)}>حذف</Btn></Gate>
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
          <div className="flex items-center gap-2"><ModeBadge mode="live" /> قراءة التفاصيل الكاملة + تعديل الاسم/الأولوية/الحالة/الرئيسي (panel_accounts)</div>
          <div className="flex items-center gap-2"><ModeBadge mode="live" /> إضافة حساب حقيقية: هاتف → OTP → 2FA → جلسة مشفرة Fernet → حفظ دائم + تفعيل فوري على المحرك (§3.3)</div>
          <div className="flex items-center gap-2"><ModeBadge mode="live" /> تفعيل/إيقاف المراقبة فوراً على المحرك + حذف الحساب (super_admin)</div>
          <div className="flex items-center gap-2"><ModeBadge mode="contract" /> اختبار اتصال دوري مجدول وإعادة مصادقة آلية — عقد §3.3 المتبقي</div>
        </div>
        <ContractNote>
          حماية الجلسات: session_encrypted/api_hash_encrypted غير قابلة للقراءة من اللوحة (سماحية أعمدة على الخادم) — لا Session String يظهر أبداً، وOTP/كلمة 2FA تعيشان في ذاكرة المحرك حصراً ولا يُسجَّلان.
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
          <label className="block text-xs text-panel-muted">المراقبة — تُطبَّق فوراً على المحرك
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

      <Modal open={deleting !== null} title={`تأكيد حذف الحساب #${deleting?.id ?? ''}`} onClose={() => setDeleting(null)}>
        {deleting && (
          <div className="space-y-3">
            <div className="text-sm">
              سيُحذف الصف <b>{deleting.phone}</b> نهائياً من panel_accounts. الحذف يتم من اللوحة مباشرة: إن كان المحرك مستيقظاً يُوقَف مراقبه فوراً، وإن كان نائماً يتوقف مراقبه تلقائياً عند استيقاظه.
              {deleting.status === 'needs_relogin' && (
                <div className="mt-2 rounded-lg border border-panel-border bg-panel-bg p-2 text-xs text-panel-muted">
                  هذا الحساب حالته needs_relogin (جلسته غير قابلة لفك التشفير) — حذفه ثم إعادة إضافته عبر المعالج أعلاه هو مسار الإصلاح الصحيح، كله من اللوحة بلا خروج.
                </div>
              )}
            </div>
            <ContractNote>الحذف لا يلمس جلسات env (ACCOUNT_*) — وهي تُدار الآن من اللوحة أيضاً: صفحة Render في القائمة الرئيسية (إضافة/تعديل/حذف/استعادة مع حفظ مزدوج على Render وSupabase).</ContractNote>
            <div className="flex justify-end gap-2">
              <Btn variant="ghost" onClick={() => setDeleting(null)}>إلغاء</Btn>
              <Btn disabled={busy} onClick={deleteRow}>{busy ? 'جارٍ الحذف…' : 'حذف نهائي'}</Btn>
            </div>
          </div>
        )}
      </Modal>

      <Modal open={wizard} title="إضافة حساب تيليجرام — تدفق حقيقي على المحرك" onClose={() => setWizard(false)}>
        <div className="space-y-3">
          {wStep === 'phone' && (
            <>
              <label className="block text-xs text-panel-muted">رقم الهاتف بالصيغة الدولية
                <Input
                  value={wPhone}
                  onChange={(e) => setWPhone(e.target.value)}
                  placeholder="+9665XXXXXXXX"
                  dir="ltr"
                  maxLength={24}
                />
              </label>
              <div className="text-xs text-panel-muted">
                سيُرسل تيليجرام رمز تأكيد إلى هذا الرقم (تطبيق تيليجرام أو SMS). الجلسة تُحفظ مشفرة في قاعدة البيانات ولا تعرض أبداً.
              </div>
              {wErr && <ErrorText>{wErr}</ErrorText>}
              <div className="flex justify-end gap-2">
                <Btn variant="ghost" onClick={cancelWizard}>إلغاء</Btn>
                <Btn disabled={wBusy || wPhone.trim().length < 8} onClick={startLink}>{wBusy ? 'جارٍ الإرسال…' : 'إرسال رمز التأكيد'}</Btn>
              </div>
            </>
          )}

          {wStep === 'code' && (
            <>
              <div className="text-xs text-panel-muted">رمز التأكيد المرسل إلى <b dir="ltr">{wPhone}</b></div>
              <Input value={wCode} onChange={(e) => setWCode(e.target.value.replace(/\D/g, ''))} placeholder="12345" dir="ltr" maxLength={10} />
              {wInfo && <div className="text-xs text-panel-muted">{wInfo}</div>}
              {wErr && <ErrorText>{wErr}</ErrorText>}
              <div className="flex justify-end gap-2">
                <Btn variant="ghost" onClick={cancelWizard}>إلغاء العملية</Btn>
                <Btn disabled={wBusy || wCode.trim().length < 4} onClick={submitCode}>{wBusy ? 'جارٍ التحقق…' : 'تحقق وإكمال الربط'}</Btn>
              </div>
            </>
          )}

          {wStep === 'password' && (
            <>
              <div className="text-xs text-panel-muted">كلمة مرور التحقق بخطوتين (2FA) لحساب <b dir="ltr">{wPhone}</b></div>
              <Input type="password" value={wPassword} onChange={(e) => setWPassword(e.target.value)} dir="ltr" maxLength={128} />
              {wInfo && <div className="text-xs text-panel-muted">{wInfo}</div>}
              {wErr && <ErrorText>{wErr}</ErrorText>}
              <ContractNote>كلمة المرور تمر كنص عابر إلى المحرك وتعيش في ذاكرته لحظياً — لا تُسجَّل ولا تُخزَّن في أي مكان.</ContractNote>
              <div className="flex justify-end gap-2">
                <Btn variant="ghost" onClick={cancelWizard}>إلغاء العملية</Btn>
                <Btn disabled={wBusy || !wPassword} onClick={submitPassword}>{wBusy ? 'جارٍ التحقق…' : 'تحقق وإكمال الربط'}</Btn>
              </div>
            </>
          )}

          {wStep === 'done' && (
            <>
              <div className="rounded-lg border border-panel-border bg-panel-bg p-3 text-sm">
                <div className="font-bold">تم ربط الحساب وفعّال الآن</div>
                {wMe && (
                  <div className="mt-2 space-y-1 text-xs">
                    <Row k="الاسم">{wMe.name}</Row>
                    <Row k="Username">{wMe.username || '—'}</Row>
                    <Row k="الهاتف" ><span dir="ltr">{wMe.phone}</span></Row>
                    <Row k="Telegram ID">{wMe.id}</Row>
                    <Row k="صف الحساب">{wRowId ? `#${wRowId}` : '—'}</Row>
                  </div>
                )}
                <div className="mt-2 text-xs text-panel-muted">
                  حُفظت الجلسة مشفرة (Fernet) في panel_accounts — تبقى صالحة عبر النشرات وإعادة التشغيل،
                  وبدأ المراقب يعمل على الحلقة الرئيسية دون إعادة تشغيل المحرك.
                </div>
              </div>
              <div className="flex justify-end">
                <Btn onClick={() => { setWizard(false); resetWizard(); }}>إغلاق</Btn>
              </div>
            </>
          )}
        </div>
      </Modal>
    </div>
  );
}

'use client';

import React from 'react';
import { useMe, Card, Btn, Input } from '@/components/ui';
import { OkText, ErrorText, Row } from '@/components/bot';

export default function AccountPage() {
  const { me } = useMe();
  const [current, setCurrent] = React.useState('');
  const [next, setNext] = React.useState('');
  const [confirm2, setConfirm2] = React.useState('');
  const [busy, setBusy] = React.useState(false);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);

  const submit = async () => {
    setOkMsg(null);
    setErrMsg(null);
    if (next !== confirm2) {
      setErrMsg('كلمة المرور الجديدة وتأكيدها غير متطابقين');
      return;
    }
    setBusy(true);
    const r = await fetch('/api/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ currentPassword: current, newPassword: next }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) {
      setOkMsg('تغيّرت كلمة المرور فعلياً في Supabase Auth — استخدمها في الدخول القادم (الجلسة الحالية تبقى صالحة حتى 8 ساعات)');
      setCurrent('');
      setNext('');
      setConfirm2('');
    } else {
      const msgs: Record<string, string> = {
        current_password_invalid: 'كلمة المرور الحالية غير صحيحة',
        csrf_rejected: 'طلب مرفوض (CSRF) — أعد المحاولة من الصفحة مباشرة',
        rate_limited: 'محاولات كثيرة — انتظر 15 دقيقة',
        invalid_body: 'كلمة المرور: 8-128 حرفاً',
      };
      setErrMsg(`فشل: ${msgs[j.error || ''] || j.error || r.status}`);
    }
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-black">حسابي — تغيير كلمة المرور</h1>

      <Card title="بيانات الجلسة">
        {me ? (
          <div>
            <Row k="البريد">{me.email}</Row>
            <Row k="الدور">{me.role}</Row>
            <Row k="الحالة">{me.enabled ? 'مفعّل' : 'موقوف'}</Row>
            <Row k="الجلسة">HMAC HttpOnly/Secure/SameSite=strict — صلاحية 8 ساعات</Row>
          </div>
        ) : (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        )}
      </Card>

      <Card title="تغيير كلمة المرور — تنفيذ حقيقي على Supabase Auth">
        <div className="max-w-md space-y-2">
          <label className="block text-xs text-panel-muted">كلمة المرور الحالية
            <Input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
          </label>
          <label className="block text-xs text-panel-muted">كلمة المرور الجديدة (8-128)
            <Input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
          </label>
          <label className="block text-xs text-panel-muted">تأكيد الجديدة
            <Input type="password" value={confirm2} onChange={(e) => setConfirm2(e.target.value)} autoComplete="new-password" />
          </label>
          <OkText>{okMsg}</OkText>
          <ErrorText>{errMsg}</ErrorText>
          <div className="flex justify-end pt-1">
            <Btn disabled={busy || !current || next.length < 8 || next !== confirm2} onClick={submit}>
              {busy ? 'جارٍ التغيير…' : 'تغيير فعلي'}
            </Btn>
          </div>
        </div>
        <div className="mt-3 text-[11px] text-panel-muted">
          التحقق: كلمة المرور الحالية تُدقق على Supabase Auth قبل أي تغيير، والتحديث يتم عبر service_role على الخادم فقط — كلمات المرور لا تُسجل في أي سجل (مموّة حرفياً)، والعملية مسجلة في audit_logs.
        </div>
      </Card>
    </div>
  );
}

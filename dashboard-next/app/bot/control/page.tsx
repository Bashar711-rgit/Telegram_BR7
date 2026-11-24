'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate } from '@/components/ui';
import { ModeBadge, ContractNote, OkText, ErrorText, STATE_LABELS } from '@/components/bot';

type CommandRecord = {
  id: string;
  type: string;
  status: string;
  requestedBy: string;
  createdAt: string;
  completedAt?: string;
  result?: Record<string, unknown>;
  error?: string;
  executionClass: string;
  note?: string;
};

type Types = Record<string, string>;
type Ledger = { ledger: CommandRecord[]; types: Types };

const COMMAND_LABELS: Record<string, string> = {
  start: 'تشغيل البوت',
  stop: 'إيقاف البوت',
  restart: 'إعادة التشغيل',
  pause: 'إيقاف المعالجة مؤقتاً (مثابر)',
  resume: 'استئناف المعالجة (مثابر)',
  reload_config: 'إعادة تحميل الإعدادات',
  maintenance_on: 'تفعيل وضع الصيانة (مثابر)',
  maintenance_off: 'تعطيل وضع الصيانة (مثابر)',
  emergency_stop_send: 'إيقاف الإرسال طوارئ (مثابر)',
  resume_send: 'استئناف الإرسال (مثابر)',
};

export default function BotControl() {
  const { me } = useMe();
  const { data, error, reload } = useApi<Ledger>('/api/bot/commands', 15000);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [confirmType, setConfirmType] = React.useState<string | null>(null);

  const issue = async (type: string) => {
    setBusy(type);
    setOkMsg(null);
    setErrMsg(null);
    const r = await fetch('/api/bot/commands', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type, idempotencyKey: `${type}-${Date.now()}` }),
    });
    const j = (await r.json()) as { ok: boolean; data?: CommandRecord; error?: string };
    setBusy(null);
    setConfirmType(null);
    if (j.ok && j.data) {
      setOkMsg(
        j.data.status === 'completed'
          ? `تم التنفيذ فعلاً (${j.data.executionClass}) — ${j.data.note || ''}`
          : `الأمر مسجل ومعلق بصدق: ${j.data.status} — ${j.data.note || 'ينتظر منفذ Render (عقد §3.1)'}`,
      );
      reload();
    } else {
      setErrMsg(`فشل: ${j.error || r.status}`);
    }
  };

  const statusNow = () => {
    // re-read status badge from ledger freshness
    return null;
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">التحكم بالتشغيل</h1>
        <span className="text-xs text-panel-muted">الأزرار تنفذ فعلاً حسب صنفها — لا نجاح وهمي</span>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ جلب السجل: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title="أزرار التحكم">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Object.keys(COMMAND_LABELS).map((t) => {
            const cls = data?.types?.[t] || 'contract';
            const dangerous = ['stop', 'restart', 'emergency_stop_send'].includes(t);
            return (
              <div key={t} className="flex items-center justify-between gap-2 rounded-lg border border-panel-border bg-panel-bg p-3">
                <div>
                  <div className="text-sm font-bold">{COMMAND_LABELS[t]}</div>
                  <div className="mt-1"><ModeBadge mode={cls as 'live' | 'live_persistent' | 'contract'} /></div>
                </div>
                <Btn
                  variant={dangerous ? 'danger' : 'primary'}
                  disabled={busy !== null || !me || !['admin', 'super_admin'].includes(me.role)}
                  onClick={() => setConfirmType(t)}
                >
                  {busy === t ? 'جارٍ…' : 'تنفيذ'}
                </Btn>
              </div>
            );
          })}
        </div>
        {me && !['admin', 'super_admin'].includes(me.role) && (
          <div className="mt-3 text-xs text-panel-muted">صلاحية التحكم التشغيلي: admin فأعلى — دورك الحالي: {me.role}</div>
        )}
      </Card>

      <ContractNote>
        <b>دلالات التنفيذ الصادقة:</b>
        <ul className="mt-1 list-disc ps-5">
          <li><b>حي</b> — إعادة التشغيل تنفذ فعلاً عبر Render API وتعيد النتيجة الحقيقية.</li>
          <li><b>حي — مثابر</b> — يكتب العلم في app_settings (dashboard:ops) ويبقى بعد إعادة التشغيل؛ العملية الحارة للمحرك تقرأ الذاكرة (monitors.py:2330/2893) وتُمَدّ من DB عند الإقلاع — لذا يُطبق عند أي إقلاع قادم. التطبيق الفوري على العملية الحارة يتطلب منفذ الأوامر (عقد §3.1).</li>
          <li><b>ينتظر Backend</b> — يُسجل في السجل والتدقيق بحالة pending_render_executor دون أي تنفيذ وهمي.</li>
        </ul>
      </ContractNote>

      <Card title="سجل الأوامر (هذه النسخة) — الدائم في سجل التدقيق">
        {!data || data.ledger.length === 0 ? (
          <div className="text-sm text-panel-muted">لا أوامر بعد في هذه النسخة — السجل الدائم في /bot/audit</div>
        ) : (
          <div className="space-y-2">
            {data.ledger.slice(0, 15).map((c) => (
              <div key={c.id} className="rounded-lg border border-panel-border bg-panel-bg p-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold">{COMMAND_LABELS[c.type] || c.type}</span>
                  <span className="text-panel-muted">{c.id.slice(0, 8)}…</span>
                  <span className={c.status === 'completed' ? 'text-panel-ok' : c.status === 'failed' ? 'text-panel-bad' : 'text-panel-warn'}>{c.status}</span>
                  <span className="text-panel-muted">{new Date(c.createdAt).toLocaleTimeString('ar')} · {c.requestedBy}</span>
                </div>
                {c.note && <div className="mt-1 text-panel-muted">{c.note}</div>}
                {c.error && <div className="mt-1 text-panel-bad">خطأ: {c.error}</div>}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Modal open={confirmType !== null} title={`تأكيد: ${confirmType ? COMMAND_LABELS[confirmType] : ''}`} onClose={() => setConfirmType(null)}>
        <p className="text-sm">سيصدر أمر <b>{confirmType ? COMMAND_LABELS[confirmType] : ''}</b> ويُسجل في التدقيق باسمك.</p>
        {confirmType && data?.types?.[confirmType] === 'contract' && (
          <ContractNote>هذا النوع ينتظر منفذ الأوامر على Render — سيُسجل بصدق كـ pending_render_executor دون تنفيذ وهمي.</ContractNote>
        )}
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setConfirmType(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy !== null} onClick={() => confirmType && issue(confirmType)}>تأكيد الإصدار</Btn>
        </div>
      </Modal>
    </div>
  );
}

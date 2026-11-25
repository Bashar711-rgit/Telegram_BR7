'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, useEngineSSE, Badge } from '@/components/ui';
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

type ReconcileReport = {
  at: string;
  panel_rows: Array<{ id: number; phone_masked: string; monitoring_enabled: boolean; status: string | null }>;
  engine_monitors: Array<{ name: string; row_id: number | null; phone_masked: string | null; source: string | null; connected: boolean; needs_relogin: boolean }>;
  enabled_no_monitor: Array<{ id: number; phone_masked: string }>;
  orphan_monitors: Array<{ row_id: number; name: string }>;
  ops: { filtering_paused: boolean; alerts_paused: boolean; note: string };
  env_mirror_only: Array<{ key: string; updatedAt: string }>;
  applied_fixes: Array<{ type: string; target: string; result: string }>;
};

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
  const { live, connected } = useEngineSSE();
  const [busy, setBusy] = React.useState<string | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [confirmType, setConfirmType] = React.useState<string | null>(null);
  const [recReport, setRecReport] = React.useState<ReconcileReport | null>(null);

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

  const runReconcile = async (fix: boolean) => {
    setBusy(fix ? 'reconcile_fix' : 'reconcile');
    setOkMsg(null);
    setErrMsg(null);
    const r = await fetch('/api/bot/reconcile', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-idempotency-key': `reconcile-${Date.now()}` },
      body: JSON.stringify({ fix }),
    });
    const j = (await r.json()) as { ok: boolean; data?: ReconcileReport; error?: string };
    setBusy(null);
    if (j.ok && j.data) {
      setRecReport(j.data);
      setOkMsg(
        fix
          ? `تمت التسوية — إصلاحات: ${j.data.applied_fixes.length || 'لا شيء مطلوب'} · أيتام: ${j.data.orphan_monitors.length} · بلا مراقب: ${j.data.enabled_no_monitor.length}`
          : `تقرير التسوية جاهز — صفوف: ${j.data.panel_rows.length} · مراقبات: ${j.data.engine_monitors.length} · أيتام: ${j.data.orphan_monitors.length}`,
      );
    } else {
      setErrMsg(`فشل التسوية: ${j.error || r.status}`);
    }
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

      <Card
        title="الحالة الحية (بث SSE كل 10 ثوانٍ)"
        actions={
          <Gate role={me?.role} min="admin">
            <Btn variant="ghost" disabled={busy !== null} onClick={() => runReconcile(false)}>تسوية الآن</Btn>
            <Btn disabled={busy !== null} onClick={() => runReconcile(true)}>تسوية + إيقاف الأيتام</Btn>
          </Gate>
        }
      >
        <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
          <div>
            <span className="text-panel-muted">البث:</span>{' '}
            {connected ? <Badge tone="ok">حي</Badge> : <Badge tone="warn">مقطوع — يعيد الاتصال</Badge>}
          </div>
          <div><span className="text-panel-muted">حالة المحرك:</span> {live?.state || '—'}</div>
          <div><span className="text-panel-muted">Uptime:</span> {live?.full?.uptime != null ? `${live.full.uptime}s` : '—'}</div>
          <div><span className="text-panel-muted">المراقبات:</span> {live?.full?.monitorsUp ?? '—'} / {live?.full?.monitorsTotal ?? '—'}</div>
          <div>
            <span className="text-panel-muted">آخر تحديث:</span> {live?.updatedAt ? new Date(live.updatedAt).toLocaleTimeString('ar') : '—'}
          </div>
        </div>
        {recReport && (
          <div className="mt-3 rounded-lg border border-panel-border bg-panel-bg p-3 text-xs">
            <div className="mb-1 font-bold">نتيجة التسوية ({new Date(recReport.at).toLocaleTimeString('ar')}):</div>
            <div>صفوف panel_accounts: {recReport.panel_rows.length} · مراقبات المحرك: {recReport.engine_monitors.length}</div>
            {recReport.enabled_no_monitor.length > 0 && (
              <div className="mt-1 text-panel-warn">مفعّلة بلا مراقب (تحتاج toggle أو إعادة تشغيل): {recReport.enabled_no_monitor.map((x) => `#${x.id}`).join('، ')}</div>
            )}
            {recReport.orphan_monitors.length > 0 && (
              <div className="mt-1 text-panel-warn">أيتام على المحرك (صفوف محذوفة): {recReport.orphan_monitors.map((x) => `#${x.row_id}`).join('، ')}</div>
            )}
            {recReport.env_mirror_only.length > 0 && (
              <div className="mt-1 text-panel-warn">انحراف مرآة env: {recReport.env_mirror_only.map((x) => x.key).join('، ')}</div>
            )}
            {recReport.applied_fixes.length > 0 && (
              <div className="mt-1">الإصلاحات: {recReport.applied_fixes.map((f) => `${f.type}#${f.target}=${f.result}`).join(' · ')}</div>
            )}
            {recReport.enabled_no_monitor.length === 0 && recReport.orphan_monitors.length === 0 && recReport.env_mirror_only.length === 0 && (
              <div className="mt-1 text-panel-ok">لا فجوات — DB والمحرك متوافقان</div>
            )}
          </div>
        )}
      </Card>

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

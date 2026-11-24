'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Select, TextArea, Badge } from '@/components/ui';
import { ModeBadge, ContractNote, OkText, ErrorText } from '@/components/bot';

type CommandRecord = {
  id: string;
  type: string;
  status: string;
  requestedBy: string;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  result?: Record<string, unknown>;
  error?: string;
  executionClass: string;
  note?: string;
};

const LIFECYCLE = ['created', 'pending', 'processing', 'completed | failed | pending_render_executor'];

export default function BotCommands() {
  const { me } = useMe();
  const canIssue = me && ['admin', 'super_admin'].includes(me.role);
  const { data, error, reload } = useApi<{ ledger: CommandRecord[]; types: Record<string, string> }>('/api/bot/commands', 15000);
  const [type, setType] = React.useState('restart');
  const [params, setParams] = React.useState('{}');
  const [idem, setIdem] = React.useState('');
  const [confirm, setConfirm] = React.useState(false);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const issue = async () => {
    setBusy(true);
    setOkMsg(null);
    setErrMsg(null);
    let parsedParams: Record<string, unknown> | undefined;
    try {
      const p = JSON.parse(params || '{}') as Record<string, unknown>;
      if (Object.keys(p).length) parsedParams = p;
    } catch {
      setErrMsg('params ليس JSON صالحاً');
      setBusy(false);
      return;
    }
    const body: Record<string, unknown> = { type };
    if (parsedParams) body.params = parsedParams;
    if (idem.trim()) body.idempotencyKey = idem.trim();
    const r = await fetch('/api/bot/commands', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = (await r.json()) as { ok: boolean; data?: CommandRecord; error?: string };
    setBusy(false);
    setConfirm(false);
    if (j.ok && j.data) {
      setOkMsg(`صدر الأمر ${j.data.id.slice(0, 8)}… — الحالة: ${j.data.status}${j.data.note ? ` — ${j.data.note}` : ''}`);
      reload();
    } else setErrMsg(`فشل: ${j.error || r.status}`);
  };

  const statusTone = (s: string) => (s === 'completed' ? 'ok' : s === 'failed' ? 'bad' : s === 'pending_render_executor' ? 'muted' : 'warn');

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">مركز الأوامر والمهام</h1>
        <Gate role={me?.role} min="admin"><Btn onClick={() => setConfirm(true)}>إصدار أمر</Btn></Gate>
      </div>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title="أنواع الأوامر وأصناف التنفيذ — الحقيقة">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {Object.entries(data?.types || {}).map(([t, cls]) => (
            <div key={t} className="flex items-center justify-between gap-2 rounded-lg border border-panel-border bg-panel-bg p-2 text-xs">
              <span className="font-bold" dir="ltr">{t}</span>
              <ModeBadge mode={cls as 'live' | 'live_persistent' | 'contract'} />
            </div>
          ))}
        </div>
      </Card>

      <Card title="دورة حياة الأمر">
        <div className="flex flex-wrap items-center gap-1 text-xs">
          {LIFECYCLE.map((s, i) => (
            <React.Fragment key={s}>
              {i > 0 && <span className="text-panel-muted">←</span>}
              <span className="rounded-md border border-panel-border bg-panel-bg px-2 py-1" dir="ltr">{s}</span>
            </React.Fragment>
          ))}
        </div>
        <ContractNote>
          الموثوقية المطبقة الآن: Idempotency (مفتاح فريد يرفض التكرار 409) · منع الأوامر المكررة · تدقيق دائم في audit_logs · قفل تنفيذ بالحالة.
          الموثوقية المنتظرة مع منفذ Render (عقد §3.1): Ack قبل التنفيذ · Timeout 30s · Retry محدود · Dead Letter Queue. لا يُعتبر تخزين الأمر دليلاً على تنفيذه — الحالة تعكس التنفيذ الفعلي فقط.
        </ContractNote>
      </Card>

      <Card title={`السجل الحي (${data?.ledger.length ?? 0} — لكل نسخة خادم؛ الدائم في التدقيق)`}>
        {(data?.ledger.length ?? 0) === 0 ? (
          <div className="text-sm text-panel-muted">لا أوامر في هذه النسخة — السجل الدائم: /bot/audit (فلتر bot_command_)</div>
        ) : (
          <div className="max-h-96 space-y-2 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            {data!.ledger.map((c) => (
              <div key={c.id} className="rounded-lg border border-panel-border bg-panel-bg p-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-bold" dir="ltr">{c.type}</span>
                  <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                  <Badge tone="muted">{c.executionClass}</Badge>
                  <span className="text-panel-muted" dir="ltr">{c.id.slice(0, 8)}…</span>
                  <span className="text-panel-muted">{c.requestedBy}</span>
                  <span className="text-panel-muted">{new Date(c.createdAt).toLocaleString('ar')}</span>
                </div>
                {c.result && <div className="mt-1 break-all text-panel-muted" dir="ltr">{JSON.stringify(c.result)}</div>}
                {c.note && <div className="mt-1 text-panel-muted">{c.note}</div>}
                {c.error && <div className="mt-1 text-panel-bad">خطأ: {c.error}</div>}
              </div>
            ))}
          </div>
        )}
      </Card>

      <Modal open={confirm} title="إصدار أمر" onClose={() => setConfirm(false)}>
        <div className="space-y-2">
          <Select value={type} onChange={(e) => setType(e.target.value)} dir="ltr">
            {Object.keys(data?.types || {}).map((t) => <option key={t} value={t}>{t}</option>)}
          </Select>
          <Input placeholder="Idempotency Key (اختياري — يمنع التكرار)" value={idem} onChange={(e) => setIdem(e.target.value)} maxLength={120} dir="ltr" />
          <TextArea rows={4} placeholder='params JSON (اختياري)' value={params} onChange={(e) => setParams(e.target.value)} dir="ltr" />
          <div className="text-[11px] text-panel-muted">
            الأصناف: live = تنفيذ فعلي فوراً · live_persistent = كتابة مثابرة فعلياً (تطبقها العملية الحارة عند الإقلاع) · contract = يُسجل بصدق كـ pending_render_executor
          </div>
          <div className="flex justify-end gap-2 pt-1">
            <Btn variant="ghost" onClick={() => setConfirm(false)}>إلغاء</Btn>
            <Btn disabled={busy} onClick={issue}>{busy ? 'جارٍ…' : 'إصدار فعلي'}</Btn>
          </div>
        </div>
      </Modal>
    </div>
  );
}

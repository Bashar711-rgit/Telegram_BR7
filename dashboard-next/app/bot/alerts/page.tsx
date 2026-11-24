'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Badge } from '@/components/ui';
import { Stat, OkText, ErrorText, ModeBadge, ContractNote } from '@/components/bot';

type Alert = {
  id: number;
  message_hash: string | null;
  chat_id: number | null;
  sender_id: number | null;
  account_name: string | null;
  keyword: string | null;
  alert_text: string | null;
  timestamp: string | null;
  decision: string | null;
  confidence: number | null;
  alert_status: string | null;
  alert_sent_at: string | null;
  notification_attempts: number | null;
  notification_error: string | null;
  button_message_url: string | null;
};

type Stats = {
  sample: { size: number };
  byStatus: Record<string, number>;
  sent: number;
  successRate: number | null;
  totalAttempts: number;
  attemptsPerAlert: number | null;
  withErrorCount: number;
  topErrors: Array<{ error: string; count: number }>;
  avgConfidence: number | null;
};

export default function BotAlerts() {
  const { me } = useMe();
  const canEdit = me && ['operator', 'admin', 'super_admin'].includes(me.role);
  const { data: stats, error: statsErr, reload: reloadStats } = useApi<Stats>('/api/bot/alerts-stats', 60000);
  const { data: rowsData, error: rowsErr, reload: reloadRows, loading } = useApi<{ rows: Alert[] }>(
    '/api/db/alerts?limit=50&order=id&desc=1',
    60000,
  );
  const [detail, setDetail] = React.useState<Alert | null>(null);
  const [blockOpen, setBlockOpen] = React.useState<Alert | null>(null);
  const [reason, setReason] = React.useState('');
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const blockSender = async () => {
    if (!blockOpen?.sender_id) return;
    setBusy(true);
    const r = await fetch('/api/db/blocked_senders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sender_id: blockOpen.sender_id, reason: reason || `محظور من بطاقة تنبيه #${blockOpen.id}` }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُظر المرسل ${blockOpen.sender_id} فعلياً في blocked_senders`); setBlockOpen(null); setReason(''); reloadStats(); }
    else setErrMsg(`فشل الحظر: ${j.error}`);
  };

  const rows = rowsData?.rows || [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">التنبيهات والتوجيه</h1>
        <Btn variant="ghost" onClick={() => { reloadStats(); reloadRows(); }}>تحديث</Btn>
      </div>
      {(statsErr || rowsErr) && <div className="text-sm text-panel-bad">خطأ: {statsErr || rowsErr}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Stat label="التنبيهات (آخر عينة)" value={stats.sample.size} />
          <Stat label="معدل نجاح الإرسال" value={stats.successRate !== null ? `${Math.round(stats.successRate * 100)}%` : '—'} tone={stats.successRate !== null && stats.successRate > 0.9 ? 'ok' : 'warn'} />
          <Stat label="متوسط المحاولات" value={stats.attemptsPerAlert ?? '—'} />
          <Stat label="بأخطاء إرسال" value={stats.withErrorCount} tone={stats.withErrorCount > 0 ? 'warn' : 'muted'} />
        </div>
      )}

      {stats && stats.topErrors.length > 0 && (
        <Card title="أهم أخطاء الإرسال (حقيقية من الصفوف)">
          <div className="space-y-1 text-xs">
            {stats.topErrors.map((e, i) => (
              <div key={i} className="flex justify-between gap-2 rounded border border-panel-border/40 bg-panel-bg p-2">
                <span className="break-all text-panel-bad" dir="ltr">{e.error}</span>
                <Badge tone="muted">{e.count}</Badge>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title={`أحدث التنبيهات (${rows.length}) — قراءة حية`}>
        {loading && !rowsData ? (
          <div className="text-sm text-panel-muted">جارٍ التحميل…</div>
        ) : rows.length === 0 ? (
          <div className="text-sm text-panel-muted">لا تنبيهات</div>
        ) : (
          <div className="max-h-96 overflow-y-auto" style={{ scrollbarWidth: 'thin' }}>
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-panel-surface">
                <tr className="text-panel-muted">
                  <th className="p-2 text-right">#</th>
                  <th className="p-2 text-right">الكلمة</th>
                  <th className="p-2 text-right">الثقة</th>
                  <th className="p-2 text-right">الحالة</th>
                  <th className="p-2 text-right">المرسل</th>
                  <th className="p-2 text-right">الوقت</th>
                  <th className="p-2"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((a) => (
                  <tr key={a.id} className="border-t border-panel-border/40 hover:bg-panel-border/20">
                    <td className="p-2">{a.id}</td>
                    <td className="p-2 max-w-32 truncate">{a.keyword || '—'}</td>
                    <td className="p-2">{a.confidence !== null && a.confidence !== undefined ? `${Math.round(a.confidence * 100)}%` : '—'}</td>
                    <td className="p-2">
                      <Badge tone={a.alert_status === 'sent' ? 'ok' : a.notification_error ? 'bad' : 'muted'}>{a.alert_status || '—'}</Badge>
                    </td>
                    <td className="p-2" dir="ltr">{a.sender_id ?? '—'}</td>
                    <td className="p-2">{a.timestamp ? new Date(a.timestamp).toLocaleString('ar') : '—'}</td>
                    <td className="p-2">
                      <div className="flex gap-1">
                        <Btn variant="ghost" onClick={() => setDetail(a)}>تفاصيل</Btn>
                        <Gate role={me?.role} min="operator">
                          {a.sender_id && <Btn variant="danger" onClick={() => setBlockOpen(a)}>حظر المرسل</Btn>}
                        </Gate>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card title="القدرات">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ModeBadge mode="live" /> قراءة + إحصاء + حظر مرسل من البطاقة
          <ModeBadge mode="contract" /> وجهات مستقلة (جدول مخصص §4) · تخصيص القالب (مجمّد في card.py) · إعادة إرسال (§3.1)
        </div>
        <ContractNote>
          التوجيه الحالي يعمل فعلياً عبر جدول rules (إجراءات send_to/alert بقيم الوجهات) — أدره من /bot/rules. بطاقة التنبيه وأزرارها عقد مجمّد في unified_alerts/card.py (بوابة الروابط الآمنة v16.3.0) — أي تخصيص يتطلب قراراً موثقاً منفصلاً.
        </ContractNote>
      </Card>

      <Modal open={detail !== null} title={`تفاصيل التنبيه #${detail?.id ?? ''}`} onClose={() => setDetail(null)}>
        {detail && (
          <div className="space-y-2 text-xs">
            <div className="rounded-lg border border-panel-border bg-panel-bg p-2 whitespace-pre-wrap">{detail.alert_text || '—'}</div>
            <div>الكلمة: {detail.keyword || '—'} · القرار: {detail.decision || '—'} · الثقة: {detail.confidence !== null ? `${Math.round(detail.confidence * 100)}%` : '—'}</div>
            <div>الحالة: {detail.alert_status || '—'} · المحاولات: {detail.notification_attempts ?? 0} {detail.alert_sent_at ? `· أُرسل: ${new Date(detail.alert_sent_at).toLocaleString('ar')}` : ''}</div>
            {detail.notification_error && <div className="text-panel-bad" dir="ltr">خطأ: {detail.notification_error}</div>}
            {detail.button_message_url && <div className="break-all">رابط الرسالة: <a className="text-panel-accent underline" href={detail.button_message_url} target="_blank" rel="noopener noreferrer">{detail.button_message_url}</a></div>}
            <div className="text-panel-muted" dir="ltr">hash: {detail.message_hash || '—'} · chat: {detail.chat_id ?? '—'} · sender: {detail.sender_id ?? '—'} · حساب: {detail.account_name || '—'}</div>
          </div>
        )}
      </Modal>

      <Modal open={blockOpen !== null} title={`حظر المرسل ${blockOpen?.sender_id ?? ''}`} onClose={() => setBlockOpen(null)}>
        <Input placeholder="سبب الحظر" value={reason} onChange={(e) => setReason(e.target.value)} maxLength={300} />
        <div className="mt-3 flex justify-end gap-2">
          <Btn variant="ghost" onClick={() => setBlockOpen(null)}>إلغاء</Btn>
          <Btn variant="danger" disabled={busy || !blockOpen?.sender_id} onClick={blockSender}>حظر فعلي</Btn>
        </div>
      </Modal>
    </div>
  );
}

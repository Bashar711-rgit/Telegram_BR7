'use client';

import React from 'react';
import { useApi, Card, Badge } from '@/components/ui';
import { Stat, ModeBadge, ContractNote, fmtUptime, ErrorText } from '@/components/bot';

type Status = {
  state: string;
  health: { reachable: boolean; status?: string; dbHealthy?: boolean };
  full: {
    uptime?: number; queueSize?: number; filterProcessed?: number; filterValid?: number;
    filterAccepted?: number; filterReview?: number; monitorsUp?: number; monitorsTotal?: number;
    floodWaitCount?: number; floodWaitRemaining?: number; resolveLatency?: number; resolveFailure?: number;
    captureSize?: number;
  } | null;
  render: { suspended?: string; latestDeployStatus?: string; updatedAt?: string } | null;
  updatedAt: string;
};

type AlertStats = {
  sample: { size: number };
  sent: number;
  successRate: number | null;
  totalAttempts: number;
  attemptsPerAlert: number | null;
  withErrorCount: number;
};

export default function BotPerformance() {
  const { data: status, error: sErr } = useApi<Status>('/api/bot/status', 15000);
  const { data: alerts, error: aErr } = useApi<AlertStats>('/api/bot/alerts-stats', 60000);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">الأداء والموارد</h1>
        <span className="text-xs text-panel-muted">تحديث تلقائي 15 ثانية · آخر: {status ? new Date(status.updatedAt).toLocaleTimeString('ar') : '—'}</span>
      </div>
      {(sErr || aErr) && <ErrorText>{sErr || aErr}</ErrorText>}

      {status?.full && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          <Stat label="مدة التشغيل" value={fmtUptime(status.full.uptime)} />
          <Stat label="حجم الطابور" value={status.full.queueSize ?? '—'} tone={(status.full.queueSize ?? 0) > 200 ? 'warn' : 'muted'} hint="قيد الإنذار: 200+" />
          <Stat label="رسائل معالَجة" value={status.full.filterProcessed ?? '—'} />
          <Stat label="مقبولة / مراجعة" value={`${status.full.filterAccepted ?? '—'} / ${status.full.filterReview ?? '—'}`} />
          <Stat label="المراقبون النشطون" value={`${status.full.monitorsUp ?? '—'} / ${status.full.monitorsTotal ?? '—'}`} tone={status.full.monitorsUp === status.full.monitorsTotal ? 'ok' : 'bad'} />
          <Stat label="أحداث FloodWait" value={status.full.floodWaitCount ?? '—'} tone={(status.full.floodWaitCount ?? 0) > 0 ? 'warn' : 'muted'} hint={status.full.floodWaitRemaining ? `متبقٍ: ${status.full.floodWaitRemaining}s` : undefined} />
          <Stat label="زمن حل المرسل" value={status.full.resolveLatency !== undefined ? `${Math.round(status.full.resolveLatency)}ms` : '—'} />
          <Stat label="فشل حل المرسل" value={status.full.resolveFailure ?? '—'} tone={(status.full.resolveFailure ?? 0) > 50 ? 'warn' : 'muted'} />
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="معدل نجاح الإشعارات (من صفوف alerts الحقيقية)">
          {alerts ? (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <Stat label="نجاح" value={alerts.successRate !== null ? `${Math.round(alerts.successRate * 100)}%` : '—'} tone={alerts.successRate !== null && alerts.successRate > 0.9 ? 'ok' : 'warn'} />
              <Stat label="أُرسل" value={alerts.sent} />
              <Stat label="محاولات كلية" value={alerts.totalAttempts} />
              <Stat label="فشل إرسال" value={alerts.withErrorCount} tone={alerts.withErrorCount > 0 ? 'bad' : 'muted'} />
            </div>
          ) : (
            <div className="text-sm text-panel-muted">—</div>
          )}
          <div className="mt-2 text-[11px] text-panel-muted">معدل الفشل = 100% − النجاح · الإرسال يُدار بعلم alerts_paused (انظر /bot/control)</div>
        </Card>

        <Card title="حالة الخدمات">
          <div className="space-y-2 text-xs">
            <div className="flex items-center justify-between rounded-lg border border-panel-border bg-panel-bg p-2">
              <span>محرك البوت (Render)</span>
              <Badge tone={status?.health.reachable ? 'ok' : 'bad'}>{status?.health.reachable ? status.health.status : 'غير متاح'}</Badge>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-panel-border bg-panel-bg p-2">
              <span>قاعدة البيانات</span>
              <Badge tone={status?.health.dbHealthy ? 'ok' : 'bad'}>{status?.health.dbHealthy ? 'ok' : 'غير سليمة'}</Badge>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-panel-border bg-panel-bg p-2">
              <span>آخر نشر Render</span>
              <Badge tone="muted">{status?.render?.latestDeployStatus || '—'}</Badge>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-panel-border bg-panel-bg p-2">
              <span>حالة الخدمة</span>
              <Badge tone={status?.render?.suspended === 'suspended' ? 'bad' : 'ok'}>{status?.render?.suspended || '—'}</Badge>
            </div>
          </div>
        </Card>
      </div>

      <Card title="إجراءات الإدارة">
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <ModeBadge mode="live" /> إعادة تشغيل المحرك (من /bot/control)
          <ModeBadge mode="contract" /> إعادة محاولة مهمة · تنظيف طابور · DLQ — عقد §3.7
        </div>
      </Card>

      <Card title="مقاييس غير متاحة حالياً (بصدق)">
        <ContractNote>
          CPU/Memory/processing_queue/dead_letters/system_health: قراءة الجداول تتطلب منح SELECT (قيد المرحلة: لا تعديل Supabase) ومقاييس الموارد تتطلب نقطة psutil أو Render metrics (عقد §3.8) — لا تُعرض أي قيم مفبركة، وستظهر فور توفر الاعتماد دون إعادة بناء.
        </ContractNote>
      </Card>
    </div>
  );
}

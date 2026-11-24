'use client';

import React from 'react';
import { useApi, Card, Btn, Badge } from '@/components/ui';
import { Stat, ModeBadge, ContractNote, ErrorText } from '@/components/bot';

type Quality = {
  sample: { size: number; window: string; cap: number };
  byDecision: Record<string, number>;
  avgConfidence: number | null;
  confidenceCount: number;
  urgentRate: number | null;
  duplicateRate: number | null;
  hashDuplicateRate: number | null;
  topReasons: Array<{ reason: string; count: number }>;
  metrics: { precision: number | null; recall: number | null; f1: number | null; note: string };
  keywordRegistry: { totalWords: number; groups: number; duplicates: number } | null;
};

export default function BotQuality() {
  const { data, error, reload } = useApi<Quality>('/api/bot/quality', 60000);
  const [showReasons, setShowReasons] = React.useState(true);

  const decisionLabels: Record<string, { ar: string; tone: 'ok' | 'warn' | 'bad' | 'muted' }> = {
    accept: { ar: 'مقبول', tone: 'ok' },
    reject: { ar: 'مرفوض', tone: 'bad' },
    review: { ar: 'مراجعة', tone: 'warn' },
    unknown: { ar: 'غير محدد', tone: 'muted' },
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">جودة التحليل — من القرارات الحقيقية</h1>
        <Btn variant="ghost" onClick={() => reload()}>تحديث</Btn>
      </div>
      {error && <ErrorText>{error}</ErrorText>}

      {data && (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="عينة القرارات" value={data.sample.size} hint="آخر الصفوف (سقف 2000)" />
            <Stat label="متوسط الثقة" value={data.avgConfidence !== null ? `${Math.round(data.avgConfidence * 100)}%` : '—'} />
            <Stat label="معدل العاجل" value={data.urgentRate !== null ? `${(data.urgentRate * 100).toFixed(1)}%` : '—'} />
            <Stat label="معدل التكرار (hash)" value={data.hashDuplicateRate !== null ? `${(data.hashDuplicateRate * 100).toFixed(1)}%` : '—'} />
          </div>

          <Card title="التوزيع الحقيقي للقرارات">
            <div className="flex flex-wrap gap-2">
              {Object.entries(data.byDecision).map(([d, c]) => {
                const lbl = decisionLabels[d] || decisionLabels.unknown;
                return (
                  <Badge key={d} tone={lbl.tone}>{lbl.ar}: {c}</Badge>
                );
              })}
            </div>
          </Card>

          {showReasons && data.topReasons.length > 0 && (
            <Card title="أهم أسباب القرار (من reasons الفعلية)">
              <div className="space-y-1 text-xs">
                {data.topReasons.map((r, i) => (
                  <div key={i} className="flex justify-between gap-2 rounded border border-panel-border/40 bg-panel-bg p-2">
                    <span className="break-all" dir="ltr">{r.reason}</span>
                    <Badge tone="muted">{r.count}</Badge>
                  </div>
                ))}
              </div>
            </Card>
          )}

          <div className="grid gap-3 lg:grid-cols-2">
            <Card title="مؤشرات الدقة — بصدق كامل">
              <div className="grid grid-cols-3 gap-2 text-center text-xs">
                <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-panel-muted">Precision</div><div className="mt-1 text-sm font-bold text-panel-muted">غير متاح</div></div>
                <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-panel-muted">Recall</div><div className="mt-1 text-sm font-bold text-panel-muted">غير متاح</div></div>
                <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-panel-muted">F1</div><div className="mt-1 text-sm font-bold text-panel-muted">غير متاح</div></div>
              </div>
              <ContractNote>{data.metrics.note}</ContractNote>
            </Card>

            <Card title="سجل الكلمات الحي">
              {data.keywordRegistry ? (
                <div className="grid grid-cols-3 gap-2 text-center text-xs">
                  <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-panel-muted">كلمات</div><div className="mt-1 text-sm font-bold">{data.keywordRegistry.totalWords}</div></div>
                  <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-panel-muted">مجموعات</div><div className="mt-1 text-sm font-bold">{data.keywordRegistry.groups}</div></div>
                  <div className="rounded-lg border border-panel-border bg-panel-bg p-3"><div className="text-panel-muted">تكرارات</div><div className="mt-1 text-sm font-bold">{data.keywordRegistry.duplicates}</div></div>
                </div>
              ) : (
                <div className="text-xs text-panel-muted">—</div>
              )}
            </Card>
          </div>

          <Card title="التغذية الراجعة (تصحيح التصنيف)">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <ModeBadge mode="contract" /> تعليم FP/FN + إعادة تحليل + حالات اختبار — عقد §3.6
            </div>
            <ContractNote>
              عمود feedback موجود فعلياً في filter_decisions لكن الكتابة عليه غير منحوة في هذه المرحلة (قيد صارم: لا تعديل قاعدة الإنتاجية) — العقد يشمل جدول message_feedback + نقطة إعادة تحليل على المحرك، والواجهة ستُربط دون إعادة بناء.
            </ContractNote>
          </Card>
        </>
      )}
    </div>
  );
}

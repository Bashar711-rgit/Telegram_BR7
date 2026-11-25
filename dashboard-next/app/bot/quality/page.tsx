'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Badge } from '@/components/ui';
import { Stat, ModeBadge, ContractNote, ErrorText, OkText } from '@/components/bot';

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

type Bench = {
  verdict?: {
    decision?: string;
    valid?: boolean;
    confidence?: number;
    score?: number;
    reasons?: string[];
    keyword?: string | null;
    indirect?: boolean;
    urgent?: boolean;
    negation_detected?: boolean;
    context_type?: string;
    intent_verb?: string | null;
    academic_object?: string | null;
    urgency_marker?: string | null;
    analysis_time_ms?: number;
  } | null;
  classification?: { classified?: boolean; subject?: string; type?: string; subject_matches?: string[]; type_matches?: string[] } | null;
  fastGate?: { any_hit?: boolean; patterns?: number } | null;
  latencyMs?: number | null;
};

const BENCH_SAMPLES = [
  'ممكن تدور لي معلم رياضيات للصف السادس ضروري جداً',
  'أحتاج ملخص درس الكسور العشرية قبل الامتحان بكرة',
  'شكراً جزيلاً على المساعدة اليوم',
  'عايز حل مسألة 12 في صف 34 من كتاب الرياضيات',
];

export default function BotQuality() {
  const { me } = useMe();
  const { data, error, reload } = useApi<Quality>('/api/bot/quality', 60000);
  const [showReasons, setShowReasons] = React.useState(true);

  // v17.2.0 §3.5: مقعد اختبار التحليل الحي
  const [benchText, setBenchText] = React.useState('');
  const [benchBusy, setBenchBusy] = React.useState(false);
  const [bench, setBench] = React.useState<Bench | null>(null);
  const [benchErr, setBenchErr] = React.useState<string | null>(null);
  const [benchOk, setBenchOk] = React.useState<string | null>(null);

  const runBench = async (text?: string) => {
    const t = (text ?? benchText).trim();
    if (!t) return;
    setBenchText(t);
    setBenchBusy(true);
    setBenchErr(null);
    setBenchOk(null);
    const r = await fetch('/api/bot/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: t.slice(0, 2000) }),
    });
    const j = (await r.json()) as { ok: boolean; error?: string; data?: Bench };
    setBenchBusy(false);
    if (j.ok && j.data) {
      setBench(j.data);
      setBenchOk(`حكم حي من المحرك خلال ${j.data.latencyMs ?? '—'}ms — بلا أي أثر جانبي`);
    } else setBenchErr(`فشل التحليل بصدق: ${j.error || r.status}`);
  };

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

          <Card
            title="مقعد اختبار التحليل الحي (§3.5 — v17.2.0)"
            actions={<ModeBadge mode="live" />}
          >
            <div className="space-y-2">
              <textarea
                rows={2}
                value={benchText}
                onChange={(e) => setBenchText(e.target.value)}
                placeholder="اكتب رسالة حقيقية وشاهد قرار الفلتر الحي على المحرك — بلا أي أثر جانبي"
                className="w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-sm text-panel-text focus:border-panel-accent focus:outline-none"
                maxLength={2000}
              />
              <div className="flex flex-wrap items-center gap-2">
                <Btn disabled={benchBusy || !benchText.trim()} onClick={() => runBench()}>
                  {benchBusy ? 'جارٍ التحليل على المحرك…' : 'تحليل حي'}
                </Btn>
                {BENCH_SAMPLES.map((s, i) => (
                  <Btn key={i} variant="ghost" disabled={benchBusy} onClick={() => runBench(s)}>
                    نمط {i + 1}
                  </Btn>
                ))}
              </div>
              <OkText>{benchOk}</OkText>
              <ErrorText>{benchErr}</ErrorText>
              {bench?.verdict && (
                <div className="rounded-lg border border-panel-border bg-panel-bg p-3 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-bold">الحكم:</span>
                    <Badge tone={bench.verdict.valid ? 'ok' : bench.verdict.decision === 'review' ? 'warn' : 'bad'}>
                      {bench.verdict.decision || '—'} {bench.verdict.valid ? '(صالح)' : ''}
                    </Badge>
                    <span>الثقة: <b>{bench.verdict.confidence != null ? `${Math.round(bench.verdict.confidence * 100)}%` : '—'}</b></span>
                    <span>الزمن: {bench.verdict.analysis_time_ms ?? '—'}ms</span>
                    {bench.fastGate && <Badge tone={bench.fastGate.any_hit ? 'warn' : 'muted'}>FastGate: {bench.fastGate.any_hit ? 'hit' : 'no-hit'} ({bench.fastGate.patterns ?? '—'} نمط)</Badge>}
                  </div>
                  {(bench.verdict.reasons?.length || 0) > 0 && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {bench.verdict.reasons!.slice(0, 10).map((r, i) => <Badge key={i} tone="muted">{r}</Badge>)}
                    </div>
                  )}
                  <div className="mt-2 flex flex-wrap gap-2 text-panel-muted">
                    <span>غير مباشر: {bench.verdict.indirect ? 'نعم' : 'لا'}</span>
                    <span>عاجل: {bench.verdict.urgent ? 'نعم' : 'لا'}</span>
                    <span>نفي: {bench.verdict.negation_detected ? 'نعم' : 'لا'}</span>
                    <span>سياق: {bench.verdict.context_type || '—'}</span>
                    {bench.verdict.academic_object && <span>كائن أكاديمي: {bench.verdict.academic_object}</span>}
                  </div>
                  {bench.classification?.classified && (
                    <div className="mt-1 text-panel-muted">التصنيف: المادة «{bench.classification.subject}» · النوع «{bench.classification.type}»</div>
                  )}
                </div>
              )}
            </div>
          </Card>

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

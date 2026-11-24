'use client';

import React from 'react';
import { useApi, useMe, Card, Btn, Modal, Gate, Input, Badge } from '@/components/ui';
import { ModeBadge, ContractNote, OkText, ErrorText } from '@/components/bot';

type Profile = { key: string; savedAt?: string; config?: unknown };

const STAGES: Array<{ n: number; t: string; real: string; mode: 'live' | 'live_persistent' | 'contract' }> = [
  { n: 1, t: 'استقبال الرسالة', real: 'monitors.py — التقاط من الحسابات (حالة العدادات حية في /health/full)', mode: 'live' },
  { n: 2, t: 'تطبيع النص', real: 'EnhancedFilter._clean (تنظيف + تطبيع عربي) — لا يُعدل من اللوحة حالياً', mode: 'contract' },
  { n: 3, t: 'الرفض الفوري', real: 'FastGate (Aho-Corasick) — عداداته حية في /health/full', mode: 'live' },
  { n: 4, t: 'كشف النية', real: 'intent_verbs + academic_objects من سجل الكلمات (إدارة حية في /bot/keywords)', mode: 'live_persistent' },
  { n: 5, t: 'كشف الكلمات', real: 'سجل dashboard:keywords_json (إدارة حية)', mode: 'live_persistent' },
  { n: 6, t: 'تحليل السياق', real: 'request_context + indirect_request (من السجل)', mode: 'live_persistent' },
  { n: 7, t: 'تصنيف الطلب', real: 'filter_decisions.classification (قراءة حية في /bot/quality)', mode: 'live' },
  { n: 8, t: 'حساب الثقة', real: 'الأوزان داخل كود المحرك — تعديل الأوزان الحار يتطلب عقد §3.2 + مخطط filter_weights', mode: 'contract' },
  { n: 9, t: 'كشف الإعلان/السبام', real: 'spam_patterns + advertisement_signals + negmem (قراءة حية للنتائج)', mode: 'live' },
  { n: 10, t: 'كشف التكرار', real: 'alert_dedup + msg_hash — معدل التكرار الحقيقي معروض في /bot/quality', mode: 'live' },
  { n: 11, t: 'القرار النهائي', real: 'filter_decisions.decision + reasons (قراءة حية)', mode: 'live' },
  { n: 12, t: 'إرسال أو رفض', real: 'alerts_paused يتحكم بالإرسال (تحكم مثابر في /bot/control)', mode: 'live_persistent' },
];

const PRESETS: Record<string, { description: string; config: Record<string, unknown> }> = {
  strict: { description: 'دقة أعلى وضوضاء أقل — يرفع حدود الثقة ويشدد الشروط', config: { mode: 'strict', confidence_min: 0.8, require_context: true, indirect_requests: false } },
  balanced: { description: 'توازن بين الاكتشاف والضوضاء', config: { mode: 'balanced', confidence_min: 0.65, require_context: false, indirect_requests: true } },
  broad: { description: 'توسيع الاكتشاف — يسمح بالطلبات غير المباشرة', config: { mode: 'broad', confidence_min: 0.5, require_context: false, indirect_requests: true } },
  custom: { description: 'تحكم يدوي كامل — عدّل JSON كما تريد', config: { mode: 'custom' } },
};

export default function BotEngine() {
  const { me } = useMe();
  const canAdmin = me && ['admin', 'super_admin'].includes(me.role);
  const { data: profiles, reload, error } = useApi<{ profiles: Profile[] }>('/api/bot/profiles');
  const [name, setName] = React.useState('');
  const [config, setConfig] = React.useState(JSON.stringify(PRESETS.balanced.config, null, 1));
  const [testText, setTestText] = React.useState('');
  const [testResult, setTestResult] = React.useState<string | null>(null);
  const [okMsg, setOkMsg] = React.useState<string | null>(null);
  const [errMsg, setErrMsg] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [confirmDel, setConfirmDel] = React.useState<string | null>(null);

  const save = async () => {
    setBusy(true);
    setOkMsg(null);
    setErrMsg(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(config);
    } catch {
      setErrMsg('الملف ليس JSON صالحاً');
      setBusy(false);
      return;
    }
    const r = await fetch('/api/bot/profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, config: parsed }) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُفظ الملف «${name}» فعلياً (KV) — تطبيقه على المحرك الحار يتطلب عقد §3.2`); reload(); }
    else setErrMsg(`فشل: ${j.error}`);
  };

  const remove = async () => {
    if (!confirmDel) return;
    setBusy(true);
    const r = await fetch('/api/bot/profiles', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: confirmDel }) });
    const j = (await r.json()) as { ok: boolean; error?: string };
    setBusy(false);
    if (j.ok) { setOkMsg(`حُذف «${confirmDel}» فعلياً`); setConfirmDel(null); reload(); } else setErrMsg(`فشل: ${j.error}`);
  };

  const runTest = () => {
    // Honest: no engine endpoint in this phase — show the exact contract instead of a fake result.
    setTestResult(
      JSON.stringify(
        {
          contract: '§3.5 POST /admin/api/engine/analyze',
          request: { text: testText },
          expectedResponse: { normalized: '…', detected_keywords: [], intent: '…', scores: { intent: 0, keyword: 0, context: 0, request: 0, negative: 0, spam: 0, final: 0 }, duplicate: { is_dup: false }, decision: 'accept|reject|review', reasons: [] },
          status: 'ينتظر منفذ المحرك على Render — لا تُقدم أي نتيجة محاكاة',
        },
        null,
        1,
      ),
    );
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-black">محرك التحليل والتصفية</h1>
      {error && <div className="text-sm text-panel-bad">خطأ: {error}</div>}
      <OkText>{okMsg}</OkText>
      <ErrorText>{errMsg}</ErrorText>

      <Card title="مراحل التحليل — الواقع عند كل مرحلة">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {STAGES.map((s) => (
            <div key={s.n} className="rounded-lg border border-panel-border bg-panel-bg p-3">
              <div className="flex items-center gap-2 text-sm font-bold">
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-panel-accent/15 text-panel-accent">{s.n}</span>
                {s.t}
              </div>
              <div className="mt-1 text-xs text-panel-muted">{s.real}</div>
              <div className="mt-1"><ModeBadge mode={s.mode} /></div>
            </div>
          ))}
        </div>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="ملفات الإعدادات (Profiles) — حفظ/مقارنة/استعادة حقيقية">
          <Gate role={me?.role} min="admin">
            <div className="space-y-2">
              <Input placeholder="اسم الملف (letters/numbers/_/-)" value={name} onChange={(e) => setName(e.target.value)} maxLength={40} dir="ltr" />
              <textarea
                rows={6}
                value={config}
                onChange={(e) => setConfig(e.target.value)}
                className="w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-xs text-panel-text focus:border-panel-accent focus:outline-none"
                dir="ltr"
              />
              <div className="flex flex-wrap gap-1">
                {Object.entries(PRESETS).map(([k, v]) => (
                  <Btn key={k} variant="ghost" onClick={() => { setConfig(JSON.stringify(v.config, null, 1)); if (!name) setName(k); }}>
                    {k}
                  </Btn>
                ))}
              </div>
              <div className="text-[11px] text-panel-muted">{PRESETS[Object.keys(PRESETS).find((k) => config.includes(`"mode": "${k}"`)) || 'custom']?.description}</div>
              <Btn disabled={busy || !name} onClick={save}>{busy ? 'جارٍ…' : 'حفظ فعلي'}</Btn>
            </div>
          </Gate>
          <div className="mt-3 space-y-2">
            <div className="text-xs font-bold text-panel-muted">الملفات المحفوظة ({profiles?.profiles.length ?? 0})</div>
            {(profiles?.profiles || []).length === 0 ? (
              <div className="text-xs text-panel-muted">لا ملفات محفوظة بعد</div>
            ) : (
              profiles!.profiles.map((p) => (
                <div key={p.key} className="flex items-center justify-between gap-2 rounded-lg border border-panel-border bg-panel-bg p-2 text-xs">
                  <div>
                    <span className="font-bold" dir="ltr">{p.key.replace('dashboard:filter_profile:', '')}</span>
                    {p.savedAt && <span className="ms-2 text-panel-muted">{new Date(p.savedAt).toLocaleString('ar')}</span>}
                    <div className="mt-0.5 max-h-16 overflow-y-auto text-panel-muted" dir="ltr" style={{ scrollbarWidth: 'thin' }}>{JSON.stringify(p.config)}</div>
                  </div>
                  <Gate role={me?.role} min="admin"><Btn variant="danger" onClick={() => setConfirmDel(p.key)}>حذف</Btn></Gate>
                </div>
              ))
            )}
          </div>
          <ContractNote>الحفظ والمقارنة والاستعادة حقيقية (KV مثابر) — تفعيل الملف على العملية الحارة يتطلب منفذ Render (عقد §3.2) — يظهر بصدق ولا يُرجع نجاح تطبيق.</ContractNote>
        </Card>

        <Card title="مقعد اختبار التحليل">
          <textarea
            rows={4}
            value={testText}
            onChange={(e) => setTestText(e.target.value)}
            placeholder="الصق رسالة لاختبارها على المحرك الحقيقي عند توفر المنفذ (عقد §3.5)"
            className="w-full rounded-lg border border-panel-border bg-panel-bg px-3 py-2 text-sm text-panel-text focus:border-panel-accent focus:outline-none"
          />
          <div className="mt-2 flex gap-2">
            <Btn disabled={!testText.trim()} onClick={runTest}>تحليل (عقد)</Btn>
          </div>
          {testResult && (
            <pre className="mt-2 max-h-72 overflow-auto rounded-lg border border-panel-border bg-panel-bg p-2 text-[11px]" dir="ltr" style={{ scrollbarWidth: 'thin' }}>{testResult}</pre>
          )}
          <ContractNote>الاختبار على المحرك الحقيقي يتطلب نقطة analyze على Render (عقد §3.5) — الواجهة جاهزة وتعرض العقد الحرفي بدل أي محاكاة.</ContractNote>
        </Card>
      </div>
    </div>
  );
}

'use client';

import React from 'react';
import { api, useMe, Btn, Card, Badge, Input, Gate, deployTone } from '@/components/ui';

type StepState = 'idle' | 'running' | 'done' | 'failed' | 'skipped' | 'canceled';

const TAG_DEFAULT = `test-tag-${new Date().toISOString().slice(0, 10).replace(/-/g, '')}`;

export default function DeployPage() {
  const { me } = useMe();
  const role = me?.role || '';

  const [tag, setTag] = React.useState(TAG_DEFAULT);
  const [states, setStates] = React.useState<StepState[]>(Array(6).fill('idle'));
  const [notes, setNotes] = React.useState<string[]>(Array(6).fill(''));
  const [running, setRunning] = React.useState(false);
  const [cancel, setCancel] = React.useState(false);
  const [result, setResult] = React.useState<{ renderUrl: string; vercelUrl: string } | null>(null);

  const setStep = (i: number, s: StepState, note?: string) => {
    setStates((prev) => prev.map((v, idx) => (idx === i ? s : v)));
    if (note !== undefined) setNotes((prev) => prev.map((v, idx) => (idx === i ? note : v)));
  };

  const waitRenderDeploy = async (deployId: string): Promise<'live' | 'failed' | 'timeout'> => {
    const deadline = Date.now() + 10 * 60 * 1000;
    while (Date.now() < deadline) {
      if (cancel) return 'timeout';
      const r = await api<{ deploys?: Array<Record<string, unknown>> }>('/api/render/deploys?limit=1');
      const d = (r.ok ? (r as { data: { deploys?: Array<Record<string, unknown>> } }).data.deploys?.[0] : null) as Record<string, unknown> | null | undefined;
      const status = String(d?.status || '');
      if (status === 'live') return 'live';
      if (['build_failed', 'activate_failed', 'canceled', 'pre_deploy_failed', 'deactivated'].includes(status)) return 'failed';
      await new Promise((res) => setTimeout(res, 10000));
    }
    return 'timeout';
  };

  const run = async () => {
    setRunning(true);
    setCancel(false);
    setResult(null);
    setStates(Array(6).fill('idle'));
    setNotes(Array(6).fill(''));

    try {
      // 1) GitHub tag
      setStep(0, 'running');
      const tagRes = await api<{ tag: string }>('/api/github/tag', { method: 'POST', body: JSON.stringify({ tag }) });
      if (tagRes.ok) setStep(0, 'done', `tag ${tag} أُنشئ على رأس main`);
      else if (tagRes.error.includes('tag_exists')) setStep(0, 'done', 'tag موجود مسبقاً — متابعة');
      else {
        setStep(0, 'failed', tagRes.error);
        setRunning(false);
        return;
      }

      // 2) CI monitoring (no trigger for tag-based workflows unless configured)
      setStep(1, 'running');
      const runs = await api<{ runs?: Array<Record<string, unknown>> }>('/api/github/runs?limit=5');
      const linkedRun = (runs.ok ? (runs as { data: { runs?: Array<Record<string, unknown>> } }).data.runs || [] : []).find((r) => String(r.headBranch || '') === tag);
      if (linkedRun) {
        setStep(1, 'done', `run مرتبط بالtag: ${String(linkedRun.status)}`);
      } else {
        setStep(1, 'skipped', 'لا workflow_dispatch/tag-trigger في المستودع — خطوة مراقبة فقط');
      }

      // 3) Render deploy
      setStep(2, 'running');
      const dep = await api<{ deployId?: string }>('/api/render/deploy', { method: 'POST', body: JSON.stringify({}) });
      if (!dep.ok) {
        setStep(2, 'failed', dep.error);
        setRunning(false);
        return;
      }
      const deployId = String((dep as { data: { deployId?: string } }).data.deployId || '');
      setStep(2, 'done', `deploy ${deployId.slice(0, 12)}… قيد البناء`);

      // 4) wait for deploy
      setStep(3, 'running', 'الاستقصاء كل 10 ثوانٍ حتى 10 دقائق');
      const verdict = await waitRenderDeploy(deployId);
      if (verdict === 'live') setStep(3, 'done', 'النشر live — البوت يعمل');
      else {
        setStep(3, 'failed', verdict === 'failed' ? 'فشل البناء — راجع Render' : 'انتهت المهلة أو أُلغي');
        setRunning(false);
        return;
      }

      // 5) bot health
      setStep(4, 'running');
      const h = await api<{ bot_health?: { reachable: boolean; status?: string; db_healthy?: boolean } }>('/api/health');
      const bh = (h.ok ? (h as { data: { bot_health?: { reachable: boolean; status?: string; db_healthy?: boolean } } }).data.bot_health : null);
      if (bh?.reachable && bh.status === 'ok') setStep(4, 'done', `/health: ok${bh.db_healthy ? ' + db_healthy' : ''}`);
      else setStep(4, bh?.reachable ? 'done' : 'failed', `health: ${bh?.status || 'unreachable'}${bh?.db_healthy ? '' : ' (db غير صحي؟)'}`);

      // 6) audit
      setStep(5, 'running');
      const a = await api<{ rows?: Array<Record<string, unknown>> }>('/api/audit?limit=5');
      const found = (a.ok ? (a as { data: { rows?: Array<Record<string, unknown>> } }).data.rows || [] : []).some((r) => String(r.action || '').includes('deploy') || String(r.action || '').includes('tag'));
      setStep(5, found ? 'done' : 'failed', found ? 'قيود التدقيق موجودة' : 'لم يُعثر على قيود تدقيق حديثة');
      setResult({ renderUrl: 'https://telegram-br7.onrender.com/health', vercelUrl: 'https://telegram-br-7.vercel.app' });
    } finally {
      setRunning(false);
    }
  };

  const steps = [
    'إنشاء tag على GitHub',
    'تشغيل/مراقبة CI',
    'تشغيل نشر Render',
    'انتظار اكتمال نشر Render',
    'فحص /health للبوت',
    'التحقق من سجل التدقيق',
  ];

  const tone = (s: StepState) =>
    s === 'done' ? 'ok' : s === 'running' ? 'warn' : s === 'failed' ? 'bad' : s === 'skipped' ? 'muted' : 'muted';

  return (
    <div className="space-y-4">
      <Gate role={role} min="operator">
        <Card title="النشر الشامل — tag + CI + Render + فحص صحة">
          <div className="mb-4 flex flex-wrap items-end gap-2">
            <div className="flex-1 min-w-60">
              <label className="mb-1 block text-xs text-panel-muted">رقم الإصدار / Tag (الاختبار: test-tag-…، الحقيقي: v14.9.0 لاحقاً)</label>
              <Input value={tag} onChange={(e) => setTag(e.target.value)} disabled={running} />
            </div>
            {!running ? (
              <Btn onClick={run}>بدء النشر</Btn>
            ) : (
              <Btn variant="danger" onClick={() => setCancel(true)}>إلغاء المراقبة</Btn>
            )}
          </div>

          <ol className="space-y-2">
            {steps.map((s, i) => (
              <li key={i} className="flex items-center justify-between gap-3 rounded-lg border border-panel-border bg-panel-bg/50 px-3 py-2">
                <div className="flex items-center gap-3">
                  <Badge tone={tone(states[i])}>{states[i]}</Badge>
                  <span className="text-sm">{s}</span>
                </div>
                <span className="max-w-60 truncate text-xs text-panel-muted" dir="ltr">{notes[i]}</span>
              </li>
            ))}
          </ol>

          {result && (
            <div className="mt-4 rounded-lg border border-panel-ok/30 bg-panel-ok/10 p-3 text-sm">
              اكتمل — الروابط:
              <ul className="mt-1 list-disc space-y-1 ps-5 text-xs">
                <li><a className="text-panel-accent underline" href={result.renderUrl} target="_blank" rel="noreferrer">Render /health</a></li>
                <li><a className="text-panel-accent underline" href={result.vercelUrl} target="_blank" rel="noreferrer">Vercel Production</a></li>
              </ul>
            </div>
          )}
        </Card>
      </Gate>
      {!me && <Card><p className="text-sm text-panel-muted">جارٍ التحميل…</p></Card>}
    </div>
  );
}

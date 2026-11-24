'use client';

import React from 'react';
import { api, useApi, useMe, Btn, Card, Badge, Modal, Input, Spinner, deployTone, Gate } from '@/components/ui';

type ServiceInfo = { configured: boolean; reachable: boolean; id?: string; name?: string; suspended?: boolean; url?: string };
type DeploysInfo = { configured: boolean; reachable: boolean; deploys?: Array<Record<string, unknown>> };
type EnvInfo = { configured: boolean; reachable: boolean; vars?: Array<{ key: string; value: string }> };

export default function RenderPage() {
  const service = useApi<ServiceInfo>('/api/render/service', 30000);
  const deploys = useApi<DeploysInfo>('/api/render/deploys?limit=10', 30000);
  const env = useApi<EnvInfo>('/api/render/env', 60000);
  const { me } = useMe();
  const role = me?.role || '';

  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [envModal, setEnvModal] = React.useState<{ mode: 'add' | 'edit'; key: string; value: string } | null>(null);
  const [confirm2, setConfirm2] = React.useState(false);
  const [deleteKey, setDeleteKey] = React.useState<string | null>(null);

  const canOperate = ['operator', 'admin', 'super_admin'].includes(role);
  const canAdmin = ['admin', 'super_admin'].includes(role);

  const doDeploy = async () => {
    setBusy(true);
    const r = await api<{ deployId?: string }>('/api/render/deploy', { method: 'POST', body: JSON.stringify({}) });
    setBusy(false);
    setNotice(r.ok ? `تم تشغيل النشر — deploy: ${(r as { data: { deployId?: string } }).data.deployId}` : `فشل: ${r.error}`);
    deploys.reload();
  };

  const doRestart = async () => {
    setBusy(true);
    const r = await api<{ via?: string }>('/api/render/restart', { method: 'POST', body: JSON.stringify({}) });
    setBusy(false);
    setNotice(r.ok ? `تمت إعادة التشغيل (${(r as { data: { via?: string } }).data.via})` : `فشل: ${r.error}`);
    deploys.reload();
  };

  const doEnvSave = async () => {
    if (!envModal) return;
    if (envModal.mode === 'edit' && !confirm2) {
      setConfirm2(true);
      return;
    }
    setBusy(true);
    const r = await api<{ deployTriggered: boolean; deployId?: string }>('/api/render/env', {
      method: 'POST',
      body: JSON.stringify({ key: envModal.key, value: envModal.value }),
    });
    setBusy(false);
    setConfirm2(false);
    setEnvModal(null);
    setNotice(r.ok ? `تم حفظ ${envModal.key}${(r as { data: { deployTriggered: boolean } }).data.deployTriggered ? ' — نشر تلقائي شُغِّل' : ''}` : `فشل: ${r.error}`);
    env.reload();
  };

  const doEnvDelete = async () => {
    if (!deleteKey) return;
    setBusy(true);
    const r = await api(`/api/render/env/${encodeURIComponent(deleteKey)}`, { method: 'DELETE' });
    setBusy(false);
    setDeleteKey(null);
    setNotice(r.ok ? `تم حذف ${deleteKey}` : `فشل: ${r.error}`);
    env.reload();
  };

  return (
    <div className="space-y-4">
      {notice && <p className="rounded-lg border border-panel-accent/30 bg-panel-accent/10 p-3 text-sm text-panel-accent">{notice}</p>}

      <Card
        title="الخدمة"
        actions={
          <Gate role={role} min="operator">
            <Btn onClick={doDeploy} disabled={busy}>نشر</Btn>
            <Btn variant="ghost" onClick={doRestart} disabled={busy}>إعادة تشغيل</Btn>
          </Gate>
        }
      >
        {service.loading ? (
          <Spinner />
        ) : (
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <div><span className="text-panel-muted">الاسم:</span> {service.data?.name || '—'}</div>
            <div><span className="text-panel-muted">معلّقة:</span> {service.data?.suspended ? 'نعم' : 'لا'}</div>
            <div><span className="text-panel-muted">الوصول:</span> {service.data?.reachable ? 'متصل' : 'غير متصل'}</div>
            <div><span className="text-panel-muted">URL:</span> {service.data?.url || '—'}</div>
          </div>
        )}
      </Card>

      <Card title="النشرات (آخر 10)">
        {deploys.loading ? (
          <Spinner />
        ) : (
          <div className="max-h-80 overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg text-panel-muted">
                <tr>
                  <th className="border-b border-panel-border px-3 py-2 text-left">id</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">status</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">commit</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">createdAt</th>
                </tr>
              </thead>
              <tbody>
                {(deploys.data?.deploys || []).map((d) => (
                  <tr key={String(d.id)}>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(d.id).slice(0, 12)}…</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5"><Badge tone={deployTone(String(d.status))}>{String(d.status)}</Badge></td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5" title={String(d.commitMessage || '')}>{String(d.commitId || '').slice(0, 7)}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{String(d.createdAt || '').slice(0, 19).replace('T', ' ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <Card
        title="متغيرات البيئة (القيم مموّهة دائماً)"
        actions={
          canAdmin && <Btn onClick={() => { setEnvModal({ mode: 'add', key: '', value: '' }); setConfirm2(false); }}>+ متغير</Btn>
        }
      >
        {env.loading ? (
          <Spinner />
        ) : (
          <div className="max-h-96 overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg text-panel-muted">
                <tr>
                  <th className="border-b border-panel-border px-3 py-2 text-left">key</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">value</th>
                  {canAdmin && <th className="border-b border-panel-border px-3 py-2 text-left">actions</th>}
                </tr>
              </thead>
              <tbody>
                {(env.data?.vars || []).map((v) => {
                  const protectedKey = /^(DATABASE_URL|SECRET_KEY|SESSION_.*)$/i.test(v.key);
                  return (
                    <tr key={v.key} className="hover:bg-panel-border/20">
                      <td className="border-b border-panel-border/50 px-3 py-1.5 font-medium">
                        {v.key} {protectedKey && <Badge tone="warn">protected</Badge>}
                      </td>
                      <td className="border-b border-panel-border/50 px-3 py-1.5 text-panel-muted">{v.value}</td>
                      {canAdmin && (
                        <td className="whitespace-nowrap border-b border-panel-border/50 px-3 py-1.5">
                          {!protectedKey && (
                            <>
                              <button className="text-panel-accent hover:underline" onClick={() => { setEnvModal({ mode: 'edit', key: v.key, value: '' }); setConfirm2(false); }}>تعديل</button>
                              <span className="mx-2 text-panel-border">|</span>
                              <button className="text-panel-bad hover:underline" onClick={() => setDeleteKey(v.key)}>حذف</button>
                            </>
                          )}
                          {protectedKey && <span className="text-panel-muted">—</span>}
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-2 text-xs text-panel-muted">المفتاح المحمي (DATABASE_URL، SECRET_KEY، SESSION_*) لا يُحذف ولا يُعدل من اللوحة. أي تعديل env يُشغّل نشراً تلقائياً.</p>
      </Card>

      <Modal open={envModal !== null} title={envModal?.mode === 'add' ? 'إضافة متغير' : `تعديل ${envModal?.key}`} onClose={() => { setEnvModal(null); setConfirm2(false); }}>
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-panel-muted">المفتاح</label>
            <Input value={envModal?.key || ''} disabled={envModal?.mode === 'edit'} onChange={(e) => setEnvModal((s) => s && { ...s, key: e.target.value })} />
          </div>
          <div>
            <label className="mb-1 block text-xs text-panel-muted">القيمة {envModal?.mode === 'edit' && '(تُستبدل بالكامل)'}</label>
            <Input value={envModal?.value || ''} onChange={(e) => setEnvModal((s) => s && { ...s, value: e.target.value })} />
          </div>
          {envModal?.mode === 'edit' && confirm2 && (
            <p className="rounded-lg border border-panel-bad/30 bg-panel-bad/10 p-2 text-xs text-panel-bad">
              تأكيد ثانٍ: التعديل سيشغّل نشراً تلقائياً للبوت (انقطاع عابر). اضغط «حفظ» مرة أخرى للتأكيد النهائي.
            </p>
          )}
          <Btn onClick={doEnvSave} disabled={busy} className="w-full">
            {envModal?.mode === 'edit' && !confirm2 ? 'متابعة → التأكيد الثاني' : busy ? 'جارٍ…' : 'حفظ + نشر تلقائي'}
          </Btn>
        </div>
      </Modal>

      <Modal open={deleteKey !== null} title="تأكيد حذف متغير" onClose={() => setDeleteKey(null)}>
        <p className="mb-4 text-sm">سيُحذف المتغير <span className="text-panel-accent">{deleteKey}</span> من Render نهائياً. المتغيرات المحمية مرفوضة تلقائياً.</p>
        <div className="flex gap-2">
          <Btn variant="danger" onClick={doEnvDelete} disabled={busy}>{busy ? 'جارٍ…' : 'حذف'}</Btn>
          <Btn variant="ghost" onClick={() => setDeleteKey(null)}>إلغاء</Btn>
        </div>
      </Modal>
    </div>
  );
}

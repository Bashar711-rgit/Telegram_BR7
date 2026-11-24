'use client';

import React from 'react';
import { api, useApi, useMe, Btn, Card, Badge, Input, Select, Modal, Spinner } from '@/components/ui';

type UserRow = { id: number; username: string; role: string; enabled: number; created_at: string; last_login_at?: string | null };
type KeysInfo = { render: Record<string, boolean>; supabase: Record<string, boolean>; github: Record<string, boolean>; vercel: Record<string, boolean>; runtime: Record<string, boolean> };

export default function SettingsPage() {
  const { me, loading } = useMe();
  const role = me?.role || '';
  const isSuper = role === 'super_admin';

  const users = useApi<{ users: UserRow[] }>(isSuper ? '/api/settings/users' : null);
  const keys = useApi<KeysInfo>(isSuper ? '/api/settings/keys' : null);
  const secrets = useApi<{ names?: string[] }>(isSuper ? '/api/github/secrets' : null);

  const [createOpen, setCreateOpen] = React.useState(false);
  const [form, setForm] = React.useState({ email: '', password: '', role: 'viewer' });
  const [notice, setNotice] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const createUser = async () => {
    setBusy(true);
    const r = await api('/api/settings/users', { method: 'POST', body: JSON.stringify(form) });
    setBusy(false);
    setNotice(r.ok ? `أُنشئ المستخدم ${form.email} بدور ${form.role}` : `فشل: ${r.error}`);
    if (r.ok) {
      setCreateOpen(false);
      setForm({ email: '', password: '', role: 'viewer' });
      users.reload();
    }
  };

  const patchUser = async (email: string, upd: { role?: string; enabled?: boolean }) => {
    setBusy(true);
    const r = await api('/api/settings/users', { method: 'PATCH', body: JSON.stringify({ email, ...upd }) });
    setBusy(false);
    setNotice(r.ok ? `حُدّث ${email}` : `فشل: ${r.error}`);
    users.reload();
  };

  if (loading) return <Card><Spinner /></Card>;
  if (!isSuper) {
    return (
      <Card title="الإعدادات">
        <p className="text-sm text-panel-muted">هذه الصفحة تتطلب دور super_admin. دورك الحالي: <Badge tone="warn">{role || 'غير معروف'}</Badge></p>
      </Card>
    );
  }

  const keyGroup = (title: string, g?: Record<string, boolean>) => (
    <div className="rounded-lg border border-panel-border p-3">
      <div className="mb-2 text-sm font-bold">{title}</div>
      <div className="flex flex-wrap gap-2">
        {Object.entries(g || {}).map(([k, v]) => (
          <span key={k} className="text-xs">
            <Badge tone={v ? 'ok' : 'bad'}>{k}: {v ? 'مضبوط' : 'غير مضبوط'}</Badge>
          </span>
        ))}
      </div>
    </div>
  );

  return (
    <div className="space-y-4">
      {notice && <p className="rounded-lg border border-panel-accent/30 bg-panel-accent/10 p-3 text-sm text-panel-accent">{notice}</p>}

      <Card
        title="المستخدمون (dashboard_users + Supabase Auth)"
        actions={<Btn onClick={() => setCreateOpen(true)}>+ مستخدم</Btn>}
      >
        {users.loading ? <Spinner /> : (
          <div className="overflow-auto rounded-lg border border-panel-border">
            <table className="w-full text-xs" dir="ltr">
              <thead className="sticky top-0 bg-panel-bg text-panel-muted">
                <tr>
                  <th className="border-b border-panel-border px-3 py-2 text-left">email</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">role</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">enabled</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">last_login</th>
                  <th className="border-b border-panel-border px-3 py-2 text-left">actions</th>
                </tr>
              </thead>
              <tbody>
                {(users.data?.users || []).map((u) => (
                  <tr key={u.id} className="hover:bg-panel-border/20">
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{u.username}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">
                      <Select value={u.role} onChange={(e) => patchUser(u.username, { role: e.target.value })} className="!w-36 !py-1 text-xs">
                        <option value="viewer">viewer</option>
                        <option value="operator">operator</option>
                        <option value="admin">admin</option>
                        <option value="super_admin">super_admin</option>
                      </Select>
                    </td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5"><Badge tone={Number(u.enabled) === 1 ? 'ok' : 'bad'}>{Number(u.enabled) === 1 ? 'مفعل' : 'معطل'}</Badge></td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">{u.last_login_at ? String(u.last_login_at).slice(0, 19).replace('T', ' ') : '—'}</td>
                    <td className="border-b border-panel-border/50 px-3 py-1.5">
                      <button className="text-panel-accent hover:underline" onClick={() => patchUser(u.username, { enabled: Number(u.enabled) !== 1 })}>
                        {Number(u.enabled) === 1 ? 'تعطيل' : 'تفعيل'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="mt-2 text-xs text-panel-muted">تعطيل المستخدم يسري فوراً على كل طلبات API التالية (فحص دوري حي من قاعدة البيانات).</p>
      </Card>

      <Card title="حالة المفاتيح (أسماء فقط — لا قيم)">
        {keys.loading ? <Spinner /> : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
            {keyGroup('Render', keys.data?.render)}
            {keyGroup('Supabase', keys.data?.supabase)}
            {keyGroup('GitHub', keys.data?.github)}
            {keyGroup('Vercel', keys.data?.vercel)}
            {keyGroup('Runtime', keys.data?.runtime)}
          </div>
        )}
      </Card>

      <Card title="أسرار GitHub (أسماء فقط)">
        {secrets.loading ? <Spinner /> : (
          <div className="flex flex-wrap gap-2">
            {(secrets.data?.names || []).map((n) => <Badge key={n} tone="muted">{n}</Badge>)}
            {(secrets.data?.names || []).length === 0 && <span className="text-sm text-panel-muted">لا أسرار على مستوى المستودع</span>}
          </div>
        )}
      </Card>

      <Modal open={createOpen} title="إنشاء مستخدم لوحة" onClose={() => setCreateOpen(false)}>
        <div className="space-y-3">
          <div>
            <label className="mb-1 block text-xs text-panel-muted">البريد الإلكتروني</label>
            <Input value={form.email} onChange={(e) => setForm((s) => ({ ...s, email: e.target.value }))} type="email" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-panel-muted">كلمة المرور (8+)</label>
            <Input value={form.password} onChange={(e) => setForm((s) => ({ ...s, password: e.target.value }))} type="password" />
          </div>
          <div>
            <label className="mb-1 block text-xs text-panel-muted">الدور</label>
            <Select value={form.role} onChange={(e) => setForm((s) => ({ ...s, role: e.target.value }))}>
              <option value="viewer">viewer</option>
              <option value="operator">operator</option>
              <option value="admin">admin</option>
              <option value="super_admin">super_admin</option>
            </Select>
          </div>
          <Btn onClick={createUser} disabled={busy || !form.email || form.password.length < 8} className="w-full">{busy ? 'جارٍ…' : 'إنشاء'}</Btn>
        </div>
      </Modal>
    </div>
  );
}

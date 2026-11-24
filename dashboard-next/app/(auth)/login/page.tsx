'use client';

import React from 'react';
import { useRouter } from 'next/navigation';
import { api, Btn, Card, Input } from '@/components/ui';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const r = await api<{ email: string; role: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    setBusy(false);
    if (r.ok) {
      router.replace('/');
      router.refresh();
    } else {
      setError(
        r.error === 'invalid_credentials'
          ? 'بيانات الدخول غير صحيحة'
          : r.error === 'rate_limited'
            ? 'محاولات كثيرة — انتظر 5 دقائق'
            : r.error === 'not_authorized'
              ? 'الحساب غير مصرح له باللوحة'
              : 'تعذر الدخول — أعد المحاولة',
      );
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <Card title="الدخول إلى لوحة التحكم" className="w-full max-w-sm">
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="mb-1 block text-xs text-panel-muted" htmlFor="email">البريد الإلكتروني</label>
            <Input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div>
            <label className="mb-1 block text-xs text-panel-muted" htmlFor="password">كلمة المرور</label>
            <Input id="password" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          {error && <p role="alert" className="rounded-lg border border-panel-bad/30 bg-panel-bad/10 p-2 text-xs text-panel-bad">{error}</p>}
          <Btn type="submit" disabled={busy} className="w-full">
            {busy ? 'جارٍ الدخول…' : 'دخول'}
          </Btn>
        </form>
      </Card>
    </main>
  );
}

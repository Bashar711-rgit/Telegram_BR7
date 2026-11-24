'use client';

import Link from 'next/link';
import { Card } from '@/components/ui';

const WRITABLE = [
  { key: 'rules', label: 'القواعد', table: 'rules', write: 'admin' },
  { key: 'sources', label: 'المصادر', table: 'sources', write: 'admin' },
  { key: 'blocked_senders', label: 'المحظورون', table: 'blocked_senders', write: 'admin' },
  { key: 'allowed_entities', label: 'المسموحون', table: 'allowed_entities', write: 'admin' },
  { key: 'app_settings', label: 'الإعدادات (KV)', table: 'app_settings', write: 'admin' },
  { key: 'panel_accounts', label: 'حسابات التشغيل', table: 'panel_accounts', write: 'admin (تعديل فقط)' },
];
const READ_ONLY = [
  { key: 'messages', label: 'الرسائل', table: 'messages' },
  { key: 'alerts', label: 'التنبيهات', table: 'alerts' },
  { key: 'filter_decisions', label: 'قرارات الفلترة', table: 'filter_decisions' },
  { key: 'audit_logs', label: 'سجل التدقيق', table: 'audit_logs' },
];

export default function DatabaseIndexPage() {
  return (
    <div className="space-y-6">
      <Card title="قاعدة البيانات — الجداول البيضاء (قابلة للتعديل: admin+)">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {WRITABLE.map((e) => (
            <Link key={e.key} href={`/database/${e.key}`} className="rounded-lg border border-panel-border p-3 transition hover:border-panel-accent">
              <div className="text-sm font-bold text-panel-text">{e.label}</div>
              <div className="mt-1 text-xs text-panel-muted">table: {e.table} · الكتابة: {e.write}</div>
            </Link>
          ))}
        </div>
      </Card>
      <Card title="الجداول الحمراء (قراءة فقط)">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {READ_ONLY.map((e) => (
            <Link key={e.key} href={`/database/${e.key}`} className="rounded-lg border border-panel-border p-3 transition hover:border-panel-accent">
              <div className="text-sm font-bold text-panel-text">{e.label}</div>
              <div className="mt-1 text-xs text-panel-muted">table: {e.table} · قراءة فقط</div>
            </Link>
          ))}
        </div>
      </Card>
    </div>
  );
}

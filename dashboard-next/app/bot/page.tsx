'use client';

import Link from 'next/link';
import { useApi, Card, Badge } from '@/components/ui';
import { Stat, STATE_LABELS, fmtUptime, ModeBadge, ContractNote } from '@/components/bot';

type Status = {
  state: string;
  derivedFrom: string[];
  health: { reachable: boolean; status?: string; dbHealthy?: boolean };
  full: { uptime?: number; queueSize?: number; filterProcessed?: number; filterValid?: number; filterAccepted?: number; filterReview?: number; monitorsUp?: number; monitorsTotal?: number; floodWaitCount?: number; resolveLatency?: number } | null;
  render: { suspended?: string; latestDeployStatus?: string } | null;
  ops: { filtering_paused: boolean; alerts_paused: boolean };
  pendingExecutorCommands: number;
  updatedAt: string;
};

type Caps = { areas: Record<string, Array<{ id: string; title: string; mode: string; depends?: string }>> };

const SECTIONS: Array<{ href: string; label: string; desc: string }> = [
  { href: '/bot/control', label: 'التحكم بالتشغيل', desc: 'أوامر المحرك — حي/مثابر/عقد' },
  { href: '/bot/accounts', label: 'الحسابات', desc: 'حسابات Telethon التشغيلية' },
  { href: '/bot/sources', label: 'مصادر المراقبة', desc: 'إدارة كاملة حية + CSV' },
  { href: '/bot/keywords', label: 'الكلمات المفتاحية', desc: 'سجل المحرك الكامل' },
  { href: '/bot/engine', label: 'محرك التصفية', desc: 'المراحل والملفات والاختبار' },
  { href: '/bot/rules', label: 'قواعد المعالجة', desc: 'CRUD حي + أولويات' },
  { href: '/bot/alerts', label: 'التنبيهات والتوجيه', desc: 'التسليم والإحصاءات' },
  { href: '/bot/blocked', label: 'الحظر والموثوقون', desc: 'قوائم حقيقية' },
  { href: '/bot/quality', label: 'جودة التحليل', desc: 'من القرارات الفعلية' },
  { href: '/bot/performance', label: 'الأداء والموارد', desc: 'مقاييس حية' },
  { href: '/bot/settings', label: 'إعدادات المحرك', desc: 'KV حقيقي + أسرار مموّهة' },
  { href: '/bot/commands', label: 'الأوامر والمهام', desc: 'دورة حياة + Idempotency' },
  { href: '/bot/audit', label: 'سجل العمليات', desc: 'تدقيق دائم' },
];

export default function BotOverview() {
  const { data: status, error, loading } = useApi<Status>('/api/bot/status', 20000);
  const { data: caps } = useApi<Caps>('/api/bot/capabilities');

  const st = status ? STATE_LABELS[status.state] || STATE_LABELS.error : null;
  const live = caps ? Object.values(caps.areas).flat().filter((c) => c.mode !== 'contract').length : 0;
  const contract = caps ? Object.values(caps.areas).flat().filter((c) => c.mode === 'contract').length : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-black">مركز التحكم بالبوت — نظرة عامة</h1>
        <span className="text-xs text-panel-muted">آخر تحديث: {status ? new Date(status.updatedAt).toLocaleTimeString('ar') : '—'}</span>
      </div>

      {error && <div className="rounded-lg border border-panel-bad/40 bg-panel-bad/10 p-3 text-sm text-panel-bad">فشل جلب الحالة: {error}</div>}

      <Card title="حالة المحرك — مشتقة لحظياً (لا قيمة مخزنة)">
        {loading && !status ? (
          <div className="text-sm text-panel-muted">جارٍ التحقق من /health و /health/full و Render…</div>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-3">
              <span className={`inline-flex items-center rounded-lg border px-3 py-1.5 text-sm font-black ${st ? `border-panel-${st.tone}/40 bg-panel-${st.tone}/15 text-panel-${st.tone}` : ''}`}>
                {st?.label || '—'}
              </span>
              {status?.health.dbHealthy === false && <Badge tone="warn">قاعدة البيانات غير سليمة</Badge>}
              {status?.ops.filtering_paused && <Badge tone="warn">المعالجة موقوفة (مثابر)</Badge>}
              {status?.ops.alerts_paused && <Badge tone="bad">الإرسال موقوف (مثابر)</Badge>}
              {status && status.pendingExecutorCommands > 0 && <Badge tone="muted">{status.pendingExecutorCommands} أمر ينتظر منفذ Render</Badge>}
            </div>
            {status && (
              <div className="text-[11px] text-panel-muted">الاشتقاق: {status.derivedFrom.join(' · ')}</div>
            )}
          </div>
        )}
      </Card>

      {status?.full && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          <Stat label="مدة التشغيل" value={fmtUptime(status.full.uptime)} />
          <Stat label="حجم الطابور" value={status.full.queueSize ?? '—'} tone={(status.full.queueSize ?? 0) > 200 ? 'warn' : 'muted'} />
          <Stat label="معالَج" value={status.full.filterProcessed ?? '—'} />
          <Stat label="مقبول" value={status.full.filterAccepted ?? '—'} tone="ok" />
          <Stat label="للمراجعة" value={status.full.filterReview ?? '—'} tone="warn" />
          <Stat label="المراقبون" value={`${status.full.monitorsUp ?? '—'}/${status.full.monitorsTotal ?? '—'}`} />
          <Stat label="أحداث FloodWait" value={status.full.floodWaitCount ?? '—'} tone={(status.full.floodWaitCount ?? 0) > 0 ? 'warn' : 'muted'} />
          <Stat label="متوسط زمن الحل" value={status.full.resolveLatency !== undefined ? `${Math.round(status.full.resolveLatency)}ms` : '—'} />
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="خريطة القدرات — الصدق أولاً">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone="ok">{live} وظيفة حية</Badge>
            <Badge tone="warn">{caps ? Object.values(caps.areas).flat().filter((c) => c.mode === 'live_persistent').length : 0} حية-مثابرة</Badge>
            <Badge tone="muted">{contract} بعقد تكامل جاهز</Badge>
          </div>
          <ContractNote>
            لا توجد أي نتيجة وهمية في هذه الوحدة: كل زر إما ينفذ فعلاً (حي) أو يكتب حالة مثابرة موثقة، أو يُظهر عقد التكامل الدقيق والاعتماد المفقود — <span className="font-bold">BOT_CONTROL_CONTRACTS.md</span>
          </ContractNote>
        </Card>
        <Card title="الخدمات">
          <div className="grid grid-cols-2 gap-2 text-xs">
            <div className="rounded-lg border border-panel-border bg-panel-bg p-2">Render: {status?.render?.suspended || '—'} · نشر: {status?.render?.latestDeployStatus || '—'}</div>
            <div className="rounded-lg border border-panel-border bg-panel-bg p-2">الصحة: {status?.health.reachable ? status.health.status : 'غير متاح'} · DB: {status?.health.dbHealthy ? 'ok' : '—'}</div>
            <Link href="/render" className="rounded-lg border border-panel-border bg-panel-bg p-2 hover:text-panel-accent">تفاصيل Render ←</Link>
            <Link href="/" className="rounded-lg border border-panel-border bg-panel-bg p-2 hover:text-panel-accent">اللوحة الرئيسية ←</Link>
          </div>
        </Card>
      </div>

      <Card title="أقسام مركز التحكم">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {SECTIONS.map((s) => (
            <Link key={s.href} href={s.href} className="rounded-lg border border-panel-border bg-panel-bg p-3 transition hover:border-panel-accent/60">
              <div className="text-sm font-bold text-panel-text">{s.label}</div>
              <div className="mt-0.5 text-xs text-panel-muted">{s.desc}</div>
            </Link>
          ))}
        </div>
      </Card>
    </div>
  );
}

/**
 * reconcile.ts — تسوية الحالة بين Supabase (المصدر الموثوق) والمحرك (Render).
 *
 * متطلب §5 من عقد المالك: «آلية Retry وReconciliation لاستعادة التوافق بين
 * حالة قاعدة البيانات وحالة البوت».
 *
 * يقارن:
 *  1) صفوف panel_accounts في DB مقابل مراقبي المحرك (بـ row_id/phone من
 *     monitor_detail v17.1.0) — يكشف:
 *     - enabled_no_monitor: صف مفعّل في DB بلا مراقب حي على المحرك
 *     - orphan_monitors: مراقب على المحرك لصف محذوف من DB (يُصلَح بـ row/stop)
 *  2) انحراف مرآة env (mirrorOnly من env-mirror)
 *  3) أعلام ops المثابرة (dashboard:ops) — تُعرض كحالة لا كتصحيح
 *
 * بلا أي نتيجة وهمية: كل عنصر يعود بدليل (row_id/name) وحالة منطقية.
 */
import { requireService } from '../supabase';
import { fetchJson } from '../http';
import { listEnvVars } from '../render';
import { mirrorList } from './env-mirror';

const RENDER_URL = () => process.env.RENDER_URL || 'https://telegram-br7-7oyu.onrender.com';

export type PanelRowView = { id: number; phone_masked: string; monitoring_enabled: boolean; status: string | null };
export type MonitorView = { name: string; row_id: number | null; phone_masked: string | null; source: string | null; connected: boolean; needs_relogin: boolean };

export type ReconcileReport = {
  at: string;
  panel_rows: PanelRowView[];
  engine_monitors: MonitorView[];
  enabled_no_monitor: Array<{ id: number; phone_masked: string }>;
  orphan_monitors: Array<{ row_id: number; name: string }>;
  ops: { filtering_paused: boolean; alerts_paused: boolean; note: string };
  env_mirror_only: Array<{ key: string; updatedAt: string }>;
  applied_fixes: Array<{ type: string; target: string; result: string }>;
};

function maskPhone(p: string | null | undefined): string | null {
  if (!p) return null;
  const digits = p.replace(/\D/g, '');
  return digits.length > 4 ? `••••${digits.slice(-4)}` : '••••';
}

async function fetchMonitorDetail(): Promise<MonitorView[] | null> {
  try {
    const r = await fetchJson<Record<string, unknown>>(`${RENDER_URL()}/health/full`, { timeoutMs: 10000 });
    if (r.status !== 200 || !r.data) return null;
    const detail = (r.data as { monitor_detail?: unknown }).monitor_detail;
    if (!Array.isArray(detail)) return null;
    return detail.map((d) => {
      const m = d as Record<string, unknown>;
      return {
        name: String(m.name || '?'),
        row_id: typeof m.row_id === 'number' && m.row_id > 0 ? m.row_id : null,
        phone_masked: maskPhone(typeof m.phone === 'string' ? m.phone : null),
        source: m.source ? String(m.source) : null,
        connected: Boolean(m.connected),
        needs_relogin: Boolean(m.needs_relogin),
      };
    });
  } catch {
    return null;
  }
}

export async function reconcile(fix: boolean): Promise<{ ok: boolean; report?: ReconcileReport; error?: string }> {
  const sb = requireService();
  const { data: rows, error } = await sb
    .from('panel_accounts')
    .select('id, phone, monitoring_enabled, status');
  if (error) return { ok: false, error: `db_read_failed: ${error.message}` };

  const panelRows: PanelRowView[] = (rows || []).map((r) => ({
    id: Number(r.id),
    phone_masked: maskPhone(String(r.phone || '')) || '••••',
    monitoring_enabled: Number(r.monitoring_enabled) === 1,
    status: r.status ? String(r.status) : null,
  }));

  const monitors = await fetchMonitorDetail();
  const applied: ReconcileReport['applied_fixes'] = [];
  // انحراف المرآة الحقيقي: مفاتيح موجودة في Supabase ولا توجد على Render
  const mir = await mirrorList();
  const envRes = await listEnvVars();
  const renderKeys = new Set(
    envRes.status === 200 && Array.isArray(envRes.data)
      ? (envRes.data as Array<{ envVar?: { key?: string } }>).map((v) => String(v?.envVar?.key || ''))
      : [],
  );
  const mirrorDrift = envRes.status === 200 ? mir.entries.filter((m) => !renderKeys.has(m.key)).map((m) => ({ key: m.key, updatedAt: m.updatedAt })) : [];

  // 1) صف مفعّل في DB بلا مراقب — يحتاج spawn (toggle hot) أو إعادة تشغيل؛ لا نستخدمه تلقائياً هنا (spawn يحتاج اتصال Telethon — يُترك لقرار المشغل)
  const enabledNoMonitor: Array<{ id: number; phone_masked: string }> = [];
  if (monitors) {
    const engineRowIds = new Set(monitors.map((m) => m.row_id).filter((x): x is number => x !== null));
    const enginePhones = new Set(
      (monitors.map((m) => m.phone_masked).filter(Boolean) as string[]),
    );
    for (const r of panelRows) {
      if (!r.monitoring_enabled) continue;
      if (engineRowIds.has(r.id) || enginePhones.has(r.phone_masked)) continue;
      enabledNoMonitor.push({ id: r.id, phone_masked: r.phone_masked });
    }
    // 2) مراقب يتيم (صف حُذف من DB) — الإصلاح: إيقافه عبر /service/accounts/row/stop (idempotent)
    const dbIds = new Set(panelRows.map((r) => r.id));
    const orphans = monitors.filter((m) => m.row_id !== null && !dbIds.has(m.row_id));
    if (fix && orphans.length) {
      const s = String(process.env.ENGINE_COMMAND_SECRET || '').trim();
      for (const o of orphans) {
        if (!s) {
          applied.push({ type: 'orphan_stop', target: String(o.row_id), result: 'skipped: ENGINE_COMMAND_SECRET غير مضبوط على اللوحة' });
          continue;
        }
        try {
          const r = await fetchJson<{ ok?: boolean; stopped?: boolean; error?: string }>(
            `${RENDER_URL()}/service/accounts/row/stop`,
            {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'x-br7-command-secret': s },
              body: JSON.stringify({ row_id: o.row_id }),
              timeoutMs: 30000,
            },
          );
          applied.push({
            type: 'orphan_stop',
            target: String(o.row_id),
            result: r.status === 200 && r.data?.ok ? `stopped:${r.data.stopped ? 'yes' : 'was_absent'}` : `failed:http_${r.status}:${r.data?.error || r.error || ''}`,
          });
        } catch (e) {
          applied.push({ type: 'orphan_stop', target: String(o.row_id), result: `failed:${String(e)}` });
        }
      }
    }
    // orphan view بدون fix: نعرضها كما هي (المراقب يتوقف عند إعادة تشغيل المحرك لأنه يقرأ DB)
    const orphanView = monitors
      .filter((m) => m.row_id !== null && !dbIds.has(m.row_id))
      .map((m) => ({ row_id: m.row_id as number, name: m.name }));
    const mir = await mirrorList();
    const { data: opsRow } = await sb.from('app_settings').select('value').eq('key', 'dashboard:ops').maybeSingle();
    let ops = { filtering_paused: false, alerts_paused: false };
    try {
      if (opsRow?.value) ops = JSON.parse(String(opsRow.value));
    } catch {
      // قيمة تالفة — نعرض الافتراضي
    }
    return {
      ok: true,
      report: {
        at: new Date().toISOString(),
        panel_rows: panelRows,
        engine_monitors: monitors,
        enabled_no_monitor: enabledNoMonitor,
        orphan_monitors: orphanView,
        ops: {
          filtering_paused: Boolean(ops.filtering_paused),
          alerts_paused: Boolean(ops.alerts_paused),
          note: 'أعلام المثابرة تُطبق على المحرك عند الإقلاع/إعادة التشغيل (hydrate_dashboard) — لا يقارنها /health/full',
        },
        env_mirror_only: mirrorDrift,
        applied_fixes: applied,
      },
    };
  }

  // المحرك غير قابل للوصول — نعيد ما نعرفه من DB فقط بصدق
  return {
    ok: true,
    report: {
      at: new Date().toISOString(),
      panel_rows: panelRows,
      engine_monitors: [],
      enabled_no_monitor: panelRows.filter((r) => r.monitoring_enabled).map((r) => ({ id: r.id, phone_masked: r.phone_masked })),
      orphan_monitors: [],
      ops: { filtering_paused: false, alerts_paused: false, note: 'المحرك غير قابل للوصول — أعلام DB فقط' },
      env_mirror_only: [],
      applied_fixes: fix ? [{ type: 'orphan_stop', target: '-', result: 'skipped: المحرك غير قابل للوصول' }] : [],
    },
  };
}

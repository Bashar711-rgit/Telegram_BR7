import { fetchJson } from '../http';
import { getService } from '../render';
import { requireService } from '../supabase';

/**
 * Real engine status — derived live from Render public health endpoints and the
 * Render service API. No cached/stored "running" value is ever trusted.
 */

const RENDER_URL = () => process.env.RENDER_URL || 'https://telegram-br7-7oyu.onrender.com';

export type EngineState =
  | 'running' | 'degraded' | 'starting' | 'stopping' | 'restarting'
  | 'stopped' | 'maintenance' | 'error';

export type BotStatus = {
  state: EngineState;
  derivedFrom: string[];
  health: { reachable: boolean; status?: string; dbHealthy?: boolean };
  full: {
    uptime?: number;
    mainClient?: string;
    monitorsUp?: number;
    monitorsTotal?: number;
    queueSize?: number;
    filterProcessed?: number;
    filterValid?: number;
    filterAccepted?: number;
    filterReview?: number;
    floodWaitCount?: number;
    floodWaitRemaining?: number;
    resolveLatency?: number;
    resolveFailure?: number;
    captureSize?: number;
  } | null;
  render: { suspended?: string; latestDeployStatus?: string; updatedAt?: string } | null;
  ops: { filtering_paused: boolean; alerts_paused: boolean };
  lastRestart?: string | null;
  updatedAt: string;
};

async function fetchFull(): Promise<Record<string, unknown> | null> {
  try {
    const r = await fetchJson<Record<string, unknown>>(`${RENDER_URL()}/health/full`, { timeoutMs: 8000 });
    return r.status === 200 && r.data ? r.data : null;
  } catch {
    return null;
  }
}

function num(o: Record<string, unknown>, path: string): number | undefined {
  let cur: unknown = o;
  for (const seg of path.split('.')) {
    if (cur && typeof cur === 'object' && seg in (cur as Record<string, unknown>)) {
      cur = (cur as Record<string, unknown>)[seg];
    } else return undefined;
  }
  return typeof cur === 'number' ? cur : undefined;
}

export async function readOpsFlags(): Promise<{ filtering_paused: boolean; alerts_paused: boolean }> {
  const fallback = { filtering_paused: false, alerts_paused: false };
  try {
    const sb = requireService();
    const { data, error } = await sb.from('app_settings').select('value').eq('key', 'dashboard:ops').maybeSingle();
    if (error || !data?.value) return fallback;
    const d = JSON.parse(String(data.value));
    return {
      filtering_paused: Boolean(d.filtering_paused),
      alerts_paused: Boolean(d.alerts_paused),
    };
  } catch {
    return fallback;
  }
}

async function writeOpsFlags(next: { filtering_paused: boolean; alerts_paused: boolean }): Promise<{ ok: boolean; error?: string }> {
  try {
    const sb = requireService();
    const { error } = await sb
      .from('app_settings')
      .update({ value: JSON.stringify(next), updated_at: new Date().toISOString() })
      .eq('key', 'dashboard:ops');
    if (error) return { ok: false, error: error.message };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

/** Persistent ops toggle — real write, engine applies on boot/restart (hydrate_dashboard). */
export async function setOpsFlag(
  flag: 'filtering_paused' | 'alerts_paused',
  value: boolean,
): Promise<{ ok: boolean; error?: string; next?: { filtering_paused: boolean; alerts_paused: boolean } }> {
  const cur = await readOpsFlags();
  const next = { ...cur, [flag]: value };
  const w = await writeOpsFlags(next);
  if (!w.ok) return { ok: false, error: w.error };
  return { ok: true, next };
}

export async function getBotStatus(): Promise<BotStatus> {
  const derivedFrom: string[] = [];
  let health: BotStatus['health'] = { reachable: false };
  try {
    const r = await fetchJson<{ status?: string; db_healthy?: boolean }>(`${RENDER_URL()}/health`, { timeoutMs: 8000 });
    if (r.status === 200 && r.data) health = { reachable: true, status: r.data.status, dbHealthy: Boolean(r.data.db_healthy) };
    else derivedFrom.push(`health_http_${r.status}`);
  } catch {
    derivedFrom.push('health_unreachable');
  }

  const full = await fetchFull();
  const svc = await getService();
  const svcData = svc.status >= 200 && svc.data ? (svc.data as Record<string, unknown>) : null;
  const suspended = svcData ? String(svcData.suspended ?? 'not_suspended') : undefined;
  const latestDeployStatus = svcData ? (svcData.latestDeployStatus as string | undefined) : undefined;
  const updatedAtSvc = svcData ? (svcData.updatedAt as string | undefined) : undefined;

  let state: EngineState;
  if (suspended === 'suspended') {
    state = 'stopped';
    derivedFrom.push('render_suspended');
  } else if (!health.reachable) {
    state = 'error';
    derivedFrom.push('health_unreachable');
  } else if (health.status === 'starting') {
    state = 'starting';
    derivedFrom.push('health_starting');
  } else if (health.status === 'degraded') {
    state = 'degraded';
    derivedFrom.push('health_degraded');
  } else if (health.reachable && health.dbHealthy === false) {
    state = 'degraded';
    derivedFrom.push('db_unhealthy');
  } else if (full && full.main_client !== undefined && String(full.main_client) !== 'up') {
    state = 'degraded';
    derivedFrom.push(`main_client_${String(full.main_client)}`);
  } else {
    state = 'running';
    derivedFrom.push('health_ok+db_ok+main_client_up');
  }

  const ops = await readOpsFlags();
  if (ops.filtering_paused || ops.alerts_paused) derivedFrom.push('ops_flag_persisted');

  return {
    state,
    derivedFrom,
    health,
    full: full
      ? {
          uptime: num(full, 'uptime'),
          mainClient: full.main_client !== undefined ? String(full.main_client) : undefined,
          monitorsUp: num(full, 'checks.monitors_up'),
          monitorsTotal: num(full, 'checks.monitors_total'),
          queueSize: num(full, 'checks.queue_size') ?? num(full, 'queue_size'),
          filterProcessed: num(full, 'checks.filter_processed'),
          filterValid: num(full, 'checks.filter_valid'),
          filterAccepted: num(full, 'checks.filter_accepted'),
          filterReview: num(full, 'checks.filter_review'),
          floodWaitCount: num(full, 'sender_intel.flood_wait_count'),
          floodWaitRemaining: num(full, 'sender_intel.flood_wait_remaining'),
          resolveLatency: num(full, 'sender_intel.average_resolve_latency'),
          resolveFailure: num(full, 'sender_intel.sender_resolve_failure'),
          captureSize: num(full, 'fast_capture.size'),
        }
      : null,
    render: { suspended, latestDeployStatus, updatedAt: updatedAtSvc },
    ops,
    updatedAt: new Date().toISOString(),
  };
}

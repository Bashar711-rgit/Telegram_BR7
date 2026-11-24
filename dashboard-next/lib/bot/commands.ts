import crypto from 'crypto';
import { restartService } from '../render';
import { setOpsFlag } from './engine';

/**
 * Command ledger — §16 lifecycle: Created → Pending → Processing → Completed / Failed.
 *
 * Honesty contract (no fake success, ever):
 * - `restart` executes a REAL Render restart and reports the real result.
 * - pause/resume/emergency_stop_send/resume_send perform a REAL persistent
 *   write (dashboard:ops in app_settings) and report `hot_path: pending_render_executor`
 *   because the running process checks in-memory flags (monitors.py:2330/2893)
 *   and hydrates from DB only at boot.
 * - Types without a live executor stay in `pending_render_executor` and are
 *   recorded in audit_logs — the ledger below is per-instance; audit is durable.
 */

export const COMMAND_TYPES = {
  start: 'contract',
  stop: 'contract',
  restart: 'live',
  pause: 'live_persistent',
  resume: 'live_persistent',
  reload_config: 'contract',
  maintenance_on: 'live_persistent',
  maintenance_off: 'live_persistent',
  emergency_stop_send: 'live_persistent',
  resume_send: 'live_persistent',
} as const;

export type CommandType = keyof typeof COMMAND_TYPES;

export type CommandRecord = {
  id: string;
  type: CommandType;
  status: 'created' | 'pending' | 'processing' | 'completed' | 'failed' | 'pending_render_executor';
  requestedBy: string;
  params?: Record<string, unknown>;
  idempotencyKey?: string;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  result?: Record<string, unknown>;
  error?: string;
  executionClass: 'live' | 'live_persistent' | 'contract';
  note?: string;
};

// Per-instance ledger (documented limitation: durable history lives in audit_logs).
const LEDGER: CommandRecord[] = [];
const IDEMPOTENCY = new Map<string, string>();
const MAX_LEDGER = 200;

function push(rec: CommandRecord) {
  LEDGER.unshift(rec);
  if (LEDGER.length > MAX_LEDGER) LEDGER.pop();
}

const OPS_MAPPING: Partial<Record<CommandType, { flag: 'filtering_paused' | 'alerts_paused'; value: boolean }>> = {
  pause: { flag: 'filtering_paused', value: true },
  resume: { flag: 'filtering_paused', value: false },
  emergency_stop_send: { flag: 'alerts_paused', value: true },
  resume_send: { flag: 'alerts_paused', value: false },
  maintenance_on: { flag: 'filtering_paused', value: true },
  maintenance_off: { flag: 'filtering_paused', value: false },
};

export type IssueResult = {
  ok: boolean;
  command?: CommandRecord;
  error?: string;
  status?: number;
};

export async function issueCommand(input: {
  type: string;
  requestedBy: string;
  params?: Record<string, unknown>;
  idempotencyKey?: string;
}): Promise<IssueResult> {
  const type = input.type as CommandType;
  if (!(type in COMMAND_TYPES)) return { ok: false, error: 'unknown_command_type', status: 400 };
  const execClass = COMMAND_TYPES[type];

  if (input.idempotencyKey) {
    const existingId = IDEMPOTENCY.get(input.idempotencyKey);
    if (existingId) {
      const existing = LEDGER.find((c) => c.id === existingId);
      if (existing) return { ok: false, error: `duplicate_command:${existingId}`, status: 409 };
    }
  }

  const rec: CommandRecord = {
    id: crypto.randomUUID(),
    type,
    status: 'created',
    requestedBy: input.requestedBy,
    params: input.params,
    idempotencyKey: input.idempotencyKey,
    createdAt: new Date().toISOString(),
    executionClass: execClass,
  };
  push(rec);
  if (input.idempotencyKey) IDEMPOTENCY.set(input.idempotencyKey, rec.id);

  rec.status = 'processing';
  rec.startedAt = new Date().toISOString();

  try {
    if (type === 'restart') {
      const r = await restartService();
      if (r.status >= 200 && r.status < 300) {
        rec.status = 'completed';
        rec.completedAt = new Date().toISOString();
        rec.result = { via: (r as { via?: string }).via || 'render_api', deployId: (r.data as { id?: string } | null)?.id ?? null };
        rec.note = 'إعادة تشغيل Render حقيقية — المحرك سيمرّ بحالة starting ثم running عند /health';
      } else {
        rec.status = 'failed';
        rec.completedAt = new Date().toISOString();
        rec.error = `render_api_${r.status}${r.error ? ':' + r.error : ''}`;
      }
      return { ok: rec.status === 'completed', command: rec, error: rec.error };
    }

    const ops = OPS_MAPPING[type];
    if (ops) {
      const w = await setOpsFlag(ops.flag, ops.value);
      if (!w.ok) {
        rec.status = 'failed';
        rec.completedAt = new Date().toISOString();
        rec.error = w.error || 'ops_write_failed';
        return { ok: false, error: rec.error, command: rec };
      }
      rec.status = 'completed';
      rec.completedAt = new Date().toISOString();
      rec.result = { persistent: true, flag: ops.flag, value: ops.value, hot_path: 'pending_render_executor' };
      rec.note =
        ops.value
          ? 'كُتب العلم في app_settings (حقيقي ومثابر) — العملية الحارة تُطبّقه عند إقلاع/إعادة تشغيل المحرك؛ التطبيق الفوري يتطلب منفذ التنفيذ (عقد §3.1)'
          : 'كُتب العلم في app_settings (حقيقي ومثابر) — إزالة الإيقاف تُطبَّق عند إقلاع/إعادة تشغيل المحرك؛ التطبيق الفوري يتطلب منفذ التنفيذ (عقد §3.1)';
      return { ok: true, command: rec };
    }

    // Contract-only command types — honest pending state, no fake success.
    rec.status = 'pending_render_executor';
    rec.note = 'ينتظر منفذ الأوامر على Render (عقد §3.1) — لا تنفيذ وهمي';
    return { ok: true, command: rec };
  } catch (e) {
    rec.status = 'failed';
    rec.completedAt = new Date().toISOString();
    rec.error = String(e);
    return { ok: false, error: rec.error, command: rec };
  }
}

export function listLedger(): CommandRecord[] {
  return [...LEDGER];
}

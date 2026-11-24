import { requireService } from './supabase';
import { logger } from './logger';

export type AuditInput = {
  actor: string;
  action: string;
  objectType?: string;
  objectId?: string | number | null;
  before?: unknown;
  after?: unknown;
  source?: string;
  ip?: string;
  userAgent?: string;
  requestId?: string;
};

/**
 * Mask values (before/after): every string becomes a bullet with its length,
 * keys and structure preserved. Numbers/booleans pass through.
 */
export function maskValue(v: unknown): unknown {
  if (v === undefined) return null;
  if (v === null) return null;
  if (typeof v === 'string') return v.length === 0 ? '' : `•••••(len:${v.length})`;
  if (Array.isArray(v)) return v.map(maskValue);
  if (typeof v === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) out[k] = maskValue(val);
    return out;
  }
  return v;
}

/** Best-effort audit write into the bot's unified audit_logs table. Never throws. */
export async function logAudit(input: AuditInput): Promise<boolean> {
  try {
    const sb = requireService();
    const { error } = await sb.from('audit_logs').insert({
      actor: input.actor,
      action: input.action,
      object_type: input.objectType ?? null,
      object_id:
        input.objectId === null || input.objectId === undefined ? null : String(input.objectId),
      old_value: input.before === undefined ? null : JSON.stringify(maskValue(input.before)),
      new_value: input.after === undefined ? null : JSON.stringify(maskValue(input.after)),
      source: input.source ?? 'vercel-dashboard',
      ip: input.ip ?? '',
      user_agent: input.userAgent ?? '',
      request_id: input.requestId ?? '',
    });
    if (error) {
      logger.error({ err: error.message, action: input.action }, 'audit_write_failed');
      return false;
    }
    return true;
  } catch (e) {
    logger.error({ err: String(e), action: input.action }, 'audit_write_failed');
    return false;
  }
}

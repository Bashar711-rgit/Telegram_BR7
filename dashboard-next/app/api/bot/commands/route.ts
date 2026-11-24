import { z } from 'zod';
import { withApi } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { issueCommand, listLedger, COMMAND_TYPES } from '@/lib/bot/commands';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

const schema = z
  .object({
    type: z.string().min(1).max(40),
    params: z.record(z.string(), z.unknown()).optional(),
    idempotencyKey: z.string().min(8).max(120).optional(),
  })
  .strict();

/** GET /api/bot/commands — ledger + execution classes. Durable history is in audit_logs. */
export const GET = withApi(
  { name: 'bot_commands_get', minRole: 'viewer', rate: { limit: 120, windowMs: 60000 } },
  async () => {
    return ok({ ledger: listLedger(), types: COMMAND_TYPES });
  },
);

/**
 * POST /api/bot/commands — issue a command (admin+).
 * restart executes live via Render; pause/resume/stop-send perform real persistent
 * writes; contract types return an honest pending_render_executor state. Never fake success.
 */
export const POST = withApi(
  { name: 'bot_commands_post', minRole: 'admin', mutating: true, rate: { limit: 20, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = schema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const res = await issueCommand({
      type: parsed.data.type,
      requestedBy: email,
      params: parsed.data.params,
      idempotencyKey: parsed.data.idempotencyKey,
    });
    if (!res.command) return err(res.error || 'issue_failed', res.status ?? 400);
    const cmd = res.command;

    await logAudit({
      actor: email,
      action: `bot_command_${parsed.data.type}`,
      objectType: 'bot_command',
      objectId: cmd.id,
      before: null,
      after: { status: cmd.status, executionClass: cmd.executionClass, result: cmd.result, error: cmd.error ?? null },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });

    if (!res.ok) return err(res.error || `command_${cmd.status}`, 500);
    return ok(cmd);
  },
);

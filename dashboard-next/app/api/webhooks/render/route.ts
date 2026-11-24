import { withWebhook } from '@/lib/api';
import { ok } from '@/lib/result';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** POST /api/webhooks/render — header x-br7-webhook-secret required. Not wired externally yet. */
export const POST = withWebhook('webhook_render', async ({ body, rid }) => {
  const b = (body || {}) as Record<string, unknown>;
  await logAudit({
    actor: 'webhook:render',
    action: 'render_webhook_event',
    objectType: 'render_webhook',
    objectId: typeof b.eventId === 'string' ? b.eventId : null,
    after: b,
    source: 'webhook:render',
    requestId: rid,
  });
  return ok({ received: true });
});

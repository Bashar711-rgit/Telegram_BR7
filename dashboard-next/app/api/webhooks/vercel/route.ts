import { withWebhook } from '@/lib/api';
import { ok } from '@/lib/result';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** POST /api/webhooks/vercel — header x-br7-webhook-secret required. Not wired externally yet. */
export const POST = withWebhook('webhook_vercel', async ({ body, rid }) => {
  const b = (body || {}) as Record<string, unknown>;
  await logAudit({
    actor: 'webhook:vercel',
    action: 'vercel_webhook_event',
    objectType: 'vercel_webhook',
    objectId: typeof b.id === 'string' ? b.id : null,
    after: b,
    source: 'webhook:vercel',
    requestId: rid,
  });
  return ok({ received: true });
});

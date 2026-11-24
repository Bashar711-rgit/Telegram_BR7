import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { capabilitiesByArea } from '@/lib/bot/capabilities';

export const dynamic = 'force-dynamic';

/** GET /api/bot/capabilities — the machine-readable LIVE vs CONTRACT truth. */
export const GET = withApi(
  { name: 'bot_capabilities', minRole: 'viewer', rate: { limit: 60, windowMs: 60000 } },
  async () => ok({ areas: capabilitiesByArea() }),
);

import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { getBotStatus } from '@/lib/bot/engine';
import { listLedger } from '@/lib/bot/commands';

export const dynamic = 'force-dynamic';

/** GET /api/bot/status — real, derived-now engine state + live counters + persistent ops flags. */
export const GET = withApi(
  { name: 'bot_status', minRole: 'viewer', rate: { limit: 120, windowMs: 60000 } },
  async () => {
    const status = await getBotStatus();
    return ok({
      ...status,
      pendingExecutorCommands: listLedger().filter((c) => c.status === 'pending_render_executor').length,
    });
  },
);

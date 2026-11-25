import { withApi } from '@/lib/api';
import { ok } from '@/lib/result';
import { engineDbInfo } from '@/lib/bot/engine-gateway';

export const dynamic = 'force-dynamic';

/**
 * GET /api/bot/db-info — v17.2.0: شفافية تخزين المحرك (قراءة).
 * يكشف بصدق إن كان المحرك على قاعدة دائمة (PostgreSQL/Supabase) أو مؤقتة
 * (SQLite على قرص Render تُمسح عند كل نشر). لا أسرار اتصال إطلاقاً.
 */
export const GET = withApi(
  { name: 'bot_db_info', minRole: 'viewer', rate: { limit: 30, windowMs: 60000 } },
  async () => {
    const r = await engineDbInfo();
    return ok({
      reachable: r.status > 0 && r.status < 500,
      dbType: r.data?.db_type ?? null,
      permanent: r.data?.permanent ?? null,
      note: r.data?.note ?? (r.status === 0 ? 'المحرك غير قابل للوصول الآن' : null),
      engineStatus: r.status,
    });
  },
);

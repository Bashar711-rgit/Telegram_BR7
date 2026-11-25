/**
 * idempotency.ts — مفاتيح عدم التكرار للعمليات الحساسة (متطلب §5 من عقد المالك).
 *
 * العميل يرسل ترويسة `x-idempotency-key` (أو Idempotency-Key) مع أي عملية
 * mutating حساسة. الخادم:
 *   - أول مفتاح → ينفذ ويخزن الاستجابة في app_settings["idem:<route>:<key>"] TTL 24 ساعة
 *   - نفس المفتاح → يعيد نفس الاستجابة المخزنة دون تنفيذ ثانٍ (x-idempotent-replay: true)
 *
 * التخزين في app_settings (جدول موجود) ببادئة idem: — لا ترحيل مخطط.
 * التنظيف: lazy purge بنسبة 10% من عمليات الكتابة (أفضل جهد، لا يفشل العملية).
 */
import { requireService } from './supabase';
import { logger } from './logger';

const PREFIX = 'idem:';
const TTL_MS = 24 * 60 * 60 * 1000;

export type StoredResponse = { status: number; body: unknown };

type StoredRecord = StoredResponse & { createdAt: string };

function storageKey(route: string, key: string): string {
  return `${PREFIX}${route}:${key}`;
}

export async function idemGet(route: string, key: string): Promise<StoredResponse | null> {
  try {
    const sb = requireService();
    const { data, error } = await sb
      .from('app_settings')
      .select('value')
      .eq('key', storageKey(route, key))
      .maybeSingle();
    if (error || !data?.value) return null;
    const rec = JSON.parse(String(data.value)) as StoredRecord;
    if (!rec || typeof rec.status !== 'number' || !rec.createdAt) return null;
    if (Date.now() - new Date(rec.createdAt).getTime() > TTL_MS) {
      void sb.from('app_settings').delete().eq('key', storageKey(route, key));
      return null;
    }
    return { status: rec.status, body: rec.body };
  } catch (e) {
    logger.warn({ err: String(e), route }, 'idempotency_get_failed');
    return null;
  }
}

export async function idemSet(route: string, key: string, res: StoredResponse): Promise<boolean> {
  try {
    const sb = requireService();
    const rec: StoredRecord = { ...res, createdAt: new Date().toISOString() };
    const { error } = await sb
      .from('app_settings')
      .upsert(
        { key: storageKey(route, key), value: JSON.stringify(rec), updated_at: new Date().toISOString() },
        { onConflict: 'key' },
      );
    if (error) {
      logger.warn({ err: error.message, route }, 'idempotency_set_failed');
      return false;
    }
    if (Math.random() < 0.1) void purgeExpired(sb);
    return true;
  } catch (e) {
    logger.warn({ err: String(e), route }, 'idempotency_set_failed');
    return false;
  }
}

/** أفضل جهد: حذف مفاتيح idem: منتهية الصلاحية (حتى 200 صف لكل نداء). */
async function purgeExpired(sb: ReturnType<typeof requireService>): Promise<void> {
  try {
    const { data } = await sb.from('app_settings').select('key, value').like('key', `${PREFIX}%`).limit(200);
    const now = Date.now();
    const dead: string[] = [];
    for (const r of data || []) {
      try {
        const rec = JSON.parse(String(r.value)) as StoredRecord;
        if (!rec?.createdAt || now - new Date(rec.createdAt).getTime() > TTL_MS) dead.push(String(r.key));
      } catch {
        dead.push(String(r.key));
      }
    }
    if (dead.length) await sb.from('app_settings').delete().in('key', dead);
  } catch {
    // التنظيف أفضل جهد فقط
  }
}

/** يستخرج مفتاح عدم التكرار من الترويسات إن وُجد. */
export function idemKeyFrom(req: Request): string | null {
  const k = (req.headers.get('x-idempotency-key') || req.headers.get('idempotency-key') || '').trim();
  if (!k || k.length > 120 || !/^[\w:.\-]+$/.test(k)) return null;
  return k;
}

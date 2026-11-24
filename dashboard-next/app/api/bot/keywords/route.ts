import { z } from 'zod';
import { withApi, parseSearch } from '@/lib/api';
import { ok, err } from '@/lib/result';
import { getRegistry, registryStats, addWord, removeWord } from '@/lib/bot/keywords';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

/** GET /api/bot/keywords — real registry view + stats + optional search & full group dump. */
export const GET = withApi(
  { name: 'bot_keywords_get', minRole: 'viewer', rate: { limit: 120, windowMs: 60000 } },
  async ({ req }) => {
    const reg = await getRegistry();
    if (!reg.ok) return err(reg.error, 500);
    const p = parseSearch(req);
    const stats = registryStats(reg.registry);
    const search = (p.search || '').trim().toLowerCase();

    const groups = stats.groups.map((g) => {
      const val = reg.registry[g.key];
      const words = Array.isArray(val) ? val : undefined;
      if (g.kind !== 'word_list' || !words) return { ...g, words: undefined, matched: 0 };
      const matched = search ? words.filter((w) => w.toLowerCase().includes(search)) : words;
      return { ...g, matchedCount: matched.length, words: search ? matched.slice(0, 50) : words.slice(0, 100) };
    });
    return ok({ stats: { totalWords: stats.totalWords, duplicates: stats.duplicates, groupCount: stats.groups.length }, groups });
  },
);

const addSchema = z
  .object({
    category: z.string().min(1).max(60).regex(/^[\w:\-]+$/),
    words: z.array(z.string().min(1).max(120)).min(1).max(200),
  })
  .strict();

/** POST /api/bot/keywords — real persistent add (single or bulk) with duplicate guard. */
export const POST = withApi(
  { name: 'bot_keywords_post', minRole: 'operator', mutating: true, rate: { limit: 30, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = addSchema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const results: Array<{ word: string; ok: boolean; error?: string }> = [];
    for (const w of parsed.data.words) {
      const r = await addWord(parsed.data.category, w);
      results.push({ word: w, ok: r.ok, error: r.error });
    }
    const added = results.filter((r) => r.ok).length;

    await logAudit({
      actor: email,
      action: `keywords_add_${parsed.data.category}`,
      objectType: 'app_settings',
      objectId: 'dashboard:keywords_json',
      before: null,
      after: { requested: parsed.data.words.length, added, failed: results.filter((r) => !r.ok) },
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });
    return ok({
      added,
      results,
      persistence: 'app_settings:dashboard:keywords_json',
      engineApplication: 'عند إقلاع/إعادة تحميل المحرك (hydrate_dashboard → reload_keywords → FastGate rebuild) — hot-reload حي يتطلب عقد §3.2',
    });
  },
);

const delSchema = z
  .object({ category: z.string().min(1).max(60).regex(/^[\w:\-]+$/), word: z.string().min(1).max(120) })
  .strict();

/** DELETE /api/bot/keywords — real persistent remove (exact match). */
export const DELETE = withApi(
  { name: 'bot_keywords_delete', minRole: 'operator', mutating: true, rate: { limit: 30, windowMs: 60000 } },
  async ({ body, email, rid, req }) => {
    const parsed = delSchema.safeParse(body);
    if (!parsed.success) return err(`invalid_body: ${parsed.error.issues[0]?.message || 'validation'}`, 400);

    const r = await removeWord(parsed.data.category, parsed.data.word);
    if (!r.ok) return err(r.error || 'remove_failed', 400);

    await logAudit({
      actor: email,
      action: `keywords_remove_${parsed.data.category}`,
      objectType: 'app_settings',
      objectId: 'dashboard:keywords_json',
      before: { word: parsed.data.word },
      after: null,
      ip: (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim(),
      userAgent: req.headers.get('user-agent') || '',
      requestId: rid,
    });
    return ok({ removed: parsed.data.word, count: r.count, engineApplication: 'عند إقلاع/إعادة تحميل المحرك' });
  },
);

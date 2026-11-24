import { requireService } from '../supabase';

/**
 * Keyword registry — lives in app_settings key `dashboard:keywords_json`.
 * The engine hydrates from this key at boot (hydrate_dashboard → writes the
 * custom file → reload_keywords → FastGate rebuild). Writes here are real and
 * persistent; hot application requires the Render executor (contract §3.2).
 */

export const KEYWORDS_KEY = 'dashboard:keywords_json';
const MAX_VALUE_CHARS = 100000; // matches app_settings update schema

export type KeywordRegistry = Record<string, string[] | Record<string, string>>;

async function readRaw(): Promise<{ ok: boolean; value?: string; error?: string }> {
  try {
    const sb = requireService();
    const { data, error } = await sb.from('app_settings').select('value').eq('key', KEYWORDS_KEY).maybeSingle();
    if (error) return { ok: false, error: error.message };
    return { ok: true, value: String(data?.value ?? '{}') };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function getRegistry(): Promise<
  { ok: true; registry: KeywordRegistry; updatedAt?: string } | { ok: false; error: string }
> {
  const raw = await readRaw();
  if (!raw.ok) return { ok: false, error: raw.error || 'read_failed' };
  try {
    const parsed = JSON.parse(raw.value || '{}') as KeywordRegistry;
    return { ok: true, registry: parsed, updatedAt: undefined };
  } catch {
    return { ok: false, error: 'registry_not_json' };
  }
}

async function writeRegistry(reg: KeywordRegistry): Promise<{ ok: boolean; error?: string }> {
  const json = JSON.stringify(reg);
  if (json.length > MAX_VALUE_CHARS) return { ok: false, error: 'registry_too_large' };
  try {
    const sb = requireService();
    const { error } = await sb
      .from('app_settings')
      .update({ value: json, updated_at: new Date().toISOString() })
      .eq('key', KEYWORDS_KEY);
    if (error) return { ok: false, error: error.message };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

const WORD_MAX = 120;

/** Add one word to a group. Group must exist (engine-owned groups are frozen; new groups need `custom:` prefix). */
export async function addWord(
  category: string,
  word: string,
): Promise<{ ok: boolean; error?: string; count?: number }> {
  if (!category || !/^[\w:\-]+$/.test(category)) return { ok: false, error: 'invalid_category' };
  const w = word.trim();
  if (!w || w.length > WORD_MAX) return { ok: false, error: 'invalid_word' };
  const reg = await getRegistry();
  if (!reg.ok) return { ok: false, error: reg.error };
  const cur = reg.registry[category];
  if (cur !== undefined && !Array.isArray(cur)) return { ok: false, error: 'category_not_word_list' };
  if (cur === undefined && !category.startsWith('custom:')) {
    return { ok: false, error: 'unknown_category_use_custom_prefix' };
  }
  const list = Array.isArray(cur) ? [...cur] : [];
  const norm = (s: string) => s.replace(/[\u064B-\u0652\u0640]/g, '').replace(/[أإآ]/g, 'ا').replace(/ى/g, 'ي').replace(/ة/g, 'ه').trim();
  if (list.some((x) => x === w)) return { ok: false, error: 'duplicate_exact' };
  if (list.some((x) => norm(x) === norm(w))) return { ok: false, error: 'duplicate_normalized' };
  list.push(w);
  reg.registry[category] = list;
  const wr = await writeRegistry(reg.registry);
  if (!wr.ok) return { ok: false, error: wr.error };
  return { ok: true, count: list.length };
}

/** Remove one word from a group (exact match only — honest failure otherwise). */
export async function removeWord(
  category: string,
  word: string,
): Promise<{ ok: boolean; error?: string; count?: number }> {
  if (!category || !/^[\w:\-]+$/.test(category)) return { ok: false, error: 'invalid_category' };
  const reg = await getRegistry();
  if (!reg.ok) return { ok: false, error: reg.error };
  const cur = reg.registry[category];
  if (!Array.isArray(cur)) return { ok: false, error: 'category_not_word_list' };
  const next = cur.filter((x) => x !== word);
  if (next.length === cur.length) return { ok: false, error: 'word_not_found' };
  reg.registry[category] = next;
  const wr = await writeRegistry(reg.registry);
  if (!wr.ok) return { ok: false, error: wr.error };
  return { ok: true, count: next.length };
}

export type RegistryStats = {
  groups: Array<{ key: string; kind: 'word_list' | 'mapping'; size: number }>;
  totalWords: number;
  duplicates: Array<{ group: string; word: string }>;
};

/** Real stats over the actual registry (exact + normalized duplicates). */
export function registryStats(reg: KeywordRegistry): RegistryStats {
  const norm = (s: string) => s.replace(/[\u064B-\u0652\u0640]/g, '').replace(/[أإآ]/g, 'ا').replace(/ى/g, 'ي').replace(/ة/g, 'ه').trim();
  const groups: RegistryStats['groups'] = [];
  const dups: RegistryStats['duplicates'] = [];
  let total = 0;
  for (const [key, val] of Object.entries(reg)) {
    if (Array.isArray(val)) {
      groups.push({ key, kind: 'word_list', size: val.length });
      total += val.length;
      const seen = new Map<string, number>();
      for (const w of val) {
        const n = norm(w);
        if (seen.has(n)) dups.push({ group: key, word: w });
        seen.set(n, 1);
      }
    } else if (val && typeof val === 'object') {
      groups.push({ key, kind: 'mapping', size: Object.keys(val).length });
    }
  }
  return { groups, totalWords: total, duplicates: dups };
}

/** Filter-profile persistence (KV) — saving is real; applying is a Render contract. */
export const PROFILE_PREFIX = 'dashboard:filter_profile:';

export async function saveProfile(name: string, config: unknown): Promise<{ ok: boolean; error?: string }> {
  if (!/^[a-z0-9_\-]{2,40}$/i.test(name)) return { ok: false, error: 'invalid_profile_name' };
  const json = JSON.stringify(config);
  if (json.length > MAX_VALUE_CHARS) return { ok: false, error: 'profile_too_large' };
  try {
    const sb = requireService();
    const key = PROFILE_PREFIX + name;
    const { data } = await sb.from('app_settings').select('key').eq('key', key).maybeSingle();
    const value = JSON.stringify({ config, savedAt: new Date().toISOString() });
    if (data?.key) {
      const { error } = await sb.from('app_settings').update({ value, updated_at: new Date().toISOString() }).eq('key', key);
      if (error) return { ok: false, error: error.message };
    } else {
      const { error } = await sb.from('app_settings').insert({ key, value });
      if (error) return { ok: false, error: error.message };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function listProfiles(): Promise<Array<{ key: string; savedAt?: string; config?: unknown }>> {
  try {
    const sb = requireService();
    const { data, error } = await sb
      .from('app_settings')
      .select('key,value')
      .like('key', PROFILE_PREFIX + '%')
      .order('key');
    if (error || !data) return [];
    return data.map((r) => {
      try {
        const parsed = JSON.parse(String(r.value)) as { config?: unknown; savedAt?: string };
        return { key: r.key, savedAt: parsed.savedAt, config: parsed.config };
      } catch {
        return { key: r.key };
      }
    });
  } catch {
    return [];
  }
}

export async function deleteProfile(name: string): Promise<{ ok: boolean; error?: string }> {
  if (!/^[a-z0-9_\-]{2,40}$/i.test(name)) return { ok: false, error: 'invalid_profile_name' };
  try {
    const sb = requireService();
    const { error } = await sb.from('app_settings').delete().eq('key', PROFILE_PREFIX + name);
    if (error) return { ok: false, error: error.message };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

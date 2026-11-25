/**
 * env-mirror.ts — مرآة Supabase لمتغيرات بيئة Render (حفظ مزدوج).
 *
 * المبدأ: القيمة تعيش على Render (مصدر التشغيل) وتُنسَخ مشفّرة في Supabase
 * (app_settings) بحيث:
 *  - كل إعداد من اللوحة يحتفظ بقيمته على الطرفين معاً (طلب المالك).
 *  - يمكن استعادة أي متغير إلى Render من المرآة عند drift/حذف خاطئ.
 *  - القيم السرية لا تُخزَّن نصاً صريحاً أبداً: AES-256-GCM بمفتاح مشتق
 *    من SESSION_SECRET (HMAC) — لا يظهر أي نص مكشوف في القاعدة ولا في الذاكرة
 *    المشتركة، ولا تعود القيم الحقيقية إلى المتصفح أبداً.
 *
 * مساحة المفاتيح: app_settings["render:env:<KEY>"] — بادئة ثابتة تُحذف/تُدمج
 * بأمان ولا تتعارض مع مفاتيح dashboard:* الموجودة.
 */
import crypto from 'crypto';
import { requireService } from '../supabase';

const PREFIX = 'render:env:';

type MirrorPayload = {
  alg: 'AES-256-GCM';
  v: string; // base64 ciphertext
  iv: string; // base64 12 bytes
  tag: string; // base64 16 bytes
  updatedAt: string;
};

function derivedKey(): Buffer {
  const s = process.env.SESSION_SECRET || '';
  if (!s) throw new Error('SESSION_SECRET_missing');
  return crypto.createHmac('sha256', s).update('br7-env-mirror-v1').digest();
}

function encrypt(plain: string): MirrorPayload {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', derivedKey(), iv);
  const ct = Buffer.concat([cipher.update(plain, 'utf8'), cipher.final()]);
  return {
    alg: 'AES-256-GCM',
    v: ct.toString('base64'),
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    updatedAt: new Date().toISOString(),
  };
}

function decrypt(p: MirrorPayload): string {
  if (p?.alg !== 'AES-256-GCM') throw new Error('mirror_alg_unknown');
  const decipher = crypto.createDecipheriv('aes-256-gcm', derivedKey(), Buffer.from(p.iv, 'base64'));
  decipher.setAuthTag(Buffer.from(p.tag, 'base64'));
  return Buffer.concat([decipher.update(Buffer.from(p.v, 'base64')), decipher.final()]).toString('utf8');
}

export function mirrorKeyFor(key: string): string {
  return `${PREFIX}${key}`;
}

export function isMirrorKey(key: string): boolean {
  return key.startsWith(PREFIX);
}

export function stripPrefix(k: string): string {
  return k.slice(PREFIX.length);
}

/** كتابة/تحديث المرآة لمتغير (يُخزَّن مشفّراً). */
export async function mirrorSet(key: string, plainValue: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const sb = requireService();
    const payload = encrypt(plainValue);
    const { error } = await sb
      .from('app_settings')
      .upsert({ key: mirrorKeyFor(key), value: JSON.stringify(payload), updated_at: new Date().toISOString() }, { onConflict: 'key' });
    if (error) return { ok: false, error: error.message };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e instanceof Error ? e.message : e) };
  }
}

/** حذف مرآة متغير (بعد حذفه من Render أو لإصلاح drift). */
export async function mirrorDelete(key: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const sb = requireService();
    const { error } = await sb.from('app_settings').delete().eq('key', mirrorKeyFor(key));
    if (error) return { ok: false, error: error.message };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e instanceof Error ? e.message : e) };
  }
}

export type MirrorEntry = { key: string; updatedAt: string; broken?: boolean };

/** قائمة المرآة — بلا أي قيم، metadata فقط. */
export async function mirrorList(): Promise<{ entries: MirrorEntry[]; error?: string }> {
  try {
    const sb = requireService();
    const { data, error } = await sb
      .from('app_settings')
      .select('key, value, updated_at')
      .like('key', `${PREFIX}%`);
    if (error) return { entries: [], error: error.message };
    const entries: MirrorEntry[] = (data || []).map((r: { key: string; value: string; updated_at: string | null }) => {
      let broken = false;
      try {
        const p = JSON.parse(String(r.value)) as MirrorPayload;
        broken = p?.alg !== 'AES-256-GCM' || !p.v || !p.iv || !p.tag;
      } catch {
        broken = true;
      }
      return { key: stripPrefix(String(r.key)), updatedAt: r.updated_at || '', broken };
    });
    return { entries };
  } catch (e) {
    return { entries: [], error: String(e instanceof Error ? e.message : e) };
  }
}

/** قراءة قيمة مفكوكة من المرآة — خادمياً فقط، لا تُعاد للعميل أبداً. */
export async function mirrorGetPlain(key: string): Promise<{ ok: boolean; value?: string; error?: string }> {
  try {
    const sb = requireService();
    const { data, error } = await sb
      .from('app_settings')
      .select('value')
      .eq('key', mirrorKeyFor(key))
      .maybeSingle();
    if (error) return { ok: false, error: error.message };
    if (!data?.value) return { ok: false, error: 'mirror_missing' };
    const p = JSON.parse(String(data.value)) as MirrorPayload;
    return { ok: true, value: decrypt(p) };
  } catch (e) {
    return { ok: false, error: String(e instanceof Error ? e.message : e) };
  }
}

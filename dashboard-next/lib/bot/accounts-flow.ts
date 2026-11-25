/**
 * accounts-flow.ts — عميل بوابة الخدمة Vercel→المحرك لتدفق ربط الحسابات الحقيقي.
 *
 * §3.3 منفذ: المحرك (Render) يملك تدفق Telethon الحقيقي عبر AccountLinkManager —
 * هذا الملف مجرد عميل HTTP رقيق فوق بوابة /service/accounts/* المحمية بسر
 * مشترك x-br7-command-secret (ENGINE_COMMAND_SECRET على الطرفين).
 *
 * قواعد صارمة:
 * - كلمة مرور 2FA ورمز OTP يمرّان هنا كنص عابر فقط ولا يُسجَّلان ولا يُخزَّنان.
 * - Session String لا تعود من المحرك أبداً (session_encrypted مخزنة Fernet لديه).
 * - فشل-مغلق: بلا ENGINE_COMMAND_SECRET لا يُرسل أي نداء (service_disabled).
 */
import { fetchJson } from '../http';

const RENDER_URL = () => process.env.RENDER_URL || 'https://telegram-br7-7oyu.onrender.com';

/** لقطة آمنة لعملية الربط تعود من المحرك — بلا عملاء ولا أسرار ولا hash. */
export type LinkOpView = {
  op_id: string;
  state: 'linking' | 'waiting_code' | 'waiting_password' | 'verifying' | 'ready' | 'active' | 'error';
  error_code?: string | null;
  error_message?: string | null;
  flood_wait?: number;
  attempts_left_code?: number;
  attempts_left_password?: number;
  phone?: string;
  me?: { id: number; name: string; username?: string | null; phone: string } | null;
  account_row_id?: number | null;
  age_seconds?: number;
};

export type EngineLinkResult = {
  ok: boolean;
  op?: LinkOpView;
  cancelled?: boolean;
  enable?: boolean;
  error_code?: string;
  error_message?: string;
  flood_wait?: number;
  error?: string;
  note?: string;
};

function secret(): string {
  return String(process.env.ENGINE_COMMAND_SECRET || '').trim();
}

async function engineCall<T = EngineLinkResult>(
  path: string,
  init: { method?: 'GET' | 'POST'; body?: unknown; timeoutMs?: number } = {},
): Promise<{ status: number; data: T | null; error?: string }> {
  const s = secret();
  if (!s) {
    return {
      status: 503,
      data: null,
      error: 'service_disabled: ENGINE_COMMAND_SECRET غير مضبوط على اللوحة',
    };
  }
  return fetchJson<T>(`${RENDER_URL()}${path}`, {
    method: init.method || 'POST',
    headers: {
      'Content-Type': 'application/json',
      'x-br7-command-secret': s,
    },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    timeoutMs: init.timeoutMs ?? 40000,
  });
}

/** الخطوة 1: إرسال رمز التأكيد إلى الرقم (يدعم "+966…" — المحرك يوحّد الصيغة). */
export function linkStart(phone: string) {
  return engineCall('/service/accounts/link_start', { body: { phone }, timeoutMs: 45000 });
}

/** الخطوة 2: إرسال OTP (يعيش في ذاكرة المحرك حصراً). */
export function linkCode(opId: string, code: string) {
  return engineCall('/service/accounts/link_code', { body: { op_id: opId, code } });
}

/** الخطوة 3 (عند الحاجة): كلمة مرور 2FA — نص عابر، لا تسجيل. */
export function linkPassword(opId: string, password: string) {
  return engineCall('/service/accounts/link_password', { body: { op_id: opId, password } });
}

/** إلغاء عملية الربط وإغلاق العميل المؤقت لدى المحرك. */
export function linkCancel(opId: string) {
  return engineCall('/service/accounts/link_cancel', { body: { op_id: opId } });
}

/** استعلام حالة العملية (TTL 15 دقيقة لدى المحرك). */
export function linkStatus(opId: string) {
  return engineCall<{ ok: boolean; op?: LinkOpView; error?: string }>(
    `/service/accounts/link_status?op_id=${encodeURIComponent(opId)}`,
    { method: 'GET', timeoutMs: 12000 },
  );
}

/** حذف صف حساب من panel_accounts + إيقاف مراقبه على الحلقة الرئيسية. */
export function rowDelete(rowId: number) {
  return engineCall('/service/accounts/row/delete', { body: { row_id: rowId }, timeoutMs: 30000 });
}

/** تفعيل/إيقاف المراقبة فوراً على الحلقة الرئيسية (spawn/stop) + تحديث الصف. */
export function rowToggle(rowId: number, enable: boolean) {
  return engineCall('/service/accounts/row/toggle', {
    body: { row_id: rowId, enable },
    timeoutMs: 150000, // spawn قد يتصل حتى 120 ثانية
  });
}

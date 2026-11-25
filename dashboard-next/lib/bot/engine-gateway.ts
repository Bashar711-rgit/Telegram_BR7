/**
 * engine-gateway.ts — عميل بوابة الخدمة Vercel→المحرك للتنفيذ الفوري (v17.2.0).
 *
 * يغطي العقود التي أصبحت حية على المحرك:
 * - §3.1  POST /service/bot/ops          تطبيق فوري لإيقاف/استئناف الفلترة/التنبيهات
 * - §3.2  POST /service/keywords/reload  إعادة بناء حية لسجل الكلمات + FastGate
 * - §3.4  POST /service/sources/check    فحص وصول حقيقي لمصدر عبر عميل تيليجرام الحي
 * - §3.5  POST /service/engine/analyze   مقعد اختبار التحليل (فلتر + مصنّف + بوابة سريعة)
 *
 * نفس قواعد accounts-flow: سر مشترك x-br7-command-secret، فشل-مغلق بلا سر،
 * لا تسريب تفاصيل، والنتيجة تعود كما هي من المحرك — لا نجاح وهمي أبداً.
 */
import { fetchJson } from '../http';

const RENDER_URL = () => process.env.RENDER_URL || 'https://telegram-br7-7oyu.onrender.com';

export type EngineGatewayResult<T = Record<string, unknown>> = {
  ok: boolean;
  status: number;
  data: T | null;
  error?: string;
};

function secret(): string {
  return String(process.env.ENGINE_COMMAND_SECRET || '').trim();
}

export async function gatewayCall<T = Record<string, unknown>>(
  path: string,
  body: unknown,
  timeoutMs = 40000,
  method: 'GET' | 'POST' = 'POST',
): Promise<EngineGatewayResult<T>> {
  const s = secret();
  if (!s) {
    return { ok: false, status: 503, data: null, error: 'service_disabled: ENGINE_COMMAND_SECRET غير مضبوط على اللوحة' };
  }
  const r = await fetchJson<T>(`${RENDER_URL()}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', 'x-br7-command-secret': s },
    body: method === 'GET' ? undefined : JSON.stringify(body ?? {}),
    timeoutMs,
  });
  const d = (r.data ?? null) as { ok?: boolean; error?: string } | null;
  return {
    ok: r.status >= 200 && r.status < 300 && d?.ok === true,
    status: r.status,
    data: r.data,
    error: r.error || (r.status >= 300 ? String(d?.error || r.status) : undefined),
  };
}

// ── §3.1: تطبيق فوري لإيقاف/استئناف المعالجة أو الإرسال على العملية الحارة ──
export type OpsInstantResult = {
  ok?: boolean;
  flag?: string;
  paused?: boolean;
  applied?: string;
  persisted?: boolean;
  ops?: { filtering_paused?: boolean; alerts_paused?: boolean; counters?: Record<string, number> };
  error?: string;
};

export function botOpsInstant(flag: 'filtering' | 'alerts', paused: boolean) {
  return gatewayCall<OpsInstantResult>('/service/bot/ops', { flag, paused }, 25000);
}

// ── §3.2: إعادة بناء حية لسجل الكلمات (السجل يُرسل من اللوحة لأنه مصدر Supabase) ──
export type KeywordsReloadResult = {
  ok?: boolean;
  reloaded?: boolean;
  groups?: number;
  source?: string;
  fast_gate?: { patterns?: number; engine?: string | number } | null;
  error?: string;
  note?: string;
};

export function keywordsHotReload(registry?: Record<string, unknown>) {
  return gatewayCall<KeywordsReloadResult>('/service/keywords/reload', { registry: registry ?? {} }, 35000);
}

// ── v17.2.0: شفافية تخزين المحرك (sqlite مؤقت / postgresql دائم) ──
export type DbInfoResult = {
  ok?: boolean;
  db_type?: string;
  permanent?: boolean;
  note?: string;
};

export function engineDbInfo() {
  return gatewayCall<DbInfoResult>('/service/db/info', {}, 15000, 'GET');
}

// ── §3.4: فحص وصول مصدر حقيقي ──
export type SourceCheckResult = {
  ok?: boolean;
  reachable?: boolean;
  entity?: { id?: number; title?: string; username?: string; type?: string } | null;
  member_status?: string;
  can_send?: boolean | null;
  error?: string;
  error_message?: string;
  note?: string;
};

export function sourcesCheck(target: { chatId?: string; username?: string }) {
  return gatewayCall<SourceCheckResult>(
    '/service/sources/check',
    { chat_id: target.chatId || '', username: target.username || '' },
    50000,
  );
}

// ── §3.5: مقعد اختبار التحليل ──
export type AnalyzeResult = {
  ok?: boolean;
  verdict?: {
    decision?: string;
    valid?: boolean;
    confidence?: number;
    score?: number;
    reasons?: string[];
    keyword?: string | null;
    indirect?: boolean;
    urgent?: boolean;
    negation_detected?: boolean;
    context_type?: string;
    intent_verb?: string | null;
    academic_object?: string | null;
    urgency_marker?: string | null;
    analysis_time_ms?: number;
  } | null;
  classification?: { classified?: boolean; subject?: string; type?: string; subject_matches?: string[]; type_matches?: string[] } | null;
  fast_gate?: { any_hit?: boolean; patterns?: number; engine?: string | number } | null;
  latency_ms?: number;
  error?: string;
};

export function engineAnalyze(text: string) {
  return gatewayCall<AnalyzeResult>('/service/engine/analyze', { text }, 20000);
}

// ── v17.2.0: منح قراءة للوحة على جداول بقائمة بيضاء (بديل SQL يدوي على Supabase) ──
export type GrantReadResult = {
  ok?: boolean;
  results?: Array<{ table: string; granted: boolean; error?: string }>;
  error?: string;
  allowed?: string[];
};

export function dbGrantRead(tables: string[]) {
  return gatewayCall<GrantReadResult>('/service/db/grant_read', { tables }, 25000);
}

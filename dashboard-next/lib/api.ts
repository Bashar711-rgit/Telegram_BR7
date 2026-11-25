import { NextRequest, NextResponse } from 'next/server';
import nodeCrypto from 'crypto';
import { verifySession, SESSION_COOKIE, SessionPayload } from './session';
import { hasMin, Role } from './auth';
import { rateLimit } from './rate-limit';
import { logger } from './logger';
import { ApiResult } from './result';
import { serviceClient } from './supabase';
import { idemGet, idemSet, idemKeyFrom } from './idempotency';

export type Ctx = {
  req: NextRequest;
  email: string;
  role: string;
  body: unknown;
  rid: string;
  seg: { params: Promise<Record<string, string>> };
};

function clientIp(req: NextRequest): string {
  return (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim();
}

/** CSRF defense line 2 (line 1 = SameSite=Strict cookie): mutations must be same-origin. */
export function sameOrigin(req: NextRequest): boolean {
  const origin = req.headers.get('origin');
  if (!origin) return false;
  try {
    return new URL(origin).host === (req.headers.get('host') || '');
  } catch {
    return false;
  }
}

/** قراءة الدور الحقيقي من dashboard_users — تُستخدم أيضاً من مسارات غير معلمة بـ withApi (SSE). */
export async function freshRole(email: string): Promise<{ role: string; enabled: boolean } | null> {
  return freshRoleRow(email);
}

async function freshRoleRow(email: string): Promise<{ role: string; enabled: boolean } | null> {
  if (!serviceClient) return null;
  const { data, error } = await serviceClient
    .from('dashboard_users')
    .select('role,enabled')
    .eq('username', email)
    .maybeSingle();
  if (error || !data) return null;
  return { role: String(data.role || ''), enabled: Number(data.enabled) === 1 };
}

type Opts = {
  name: string;
  minRole?: Role;
  mutating?: boolean;
  rate?: { limit: number; windowMs: number };
  /** تفعيل مفاتيح عدم التكرار: العميل يرسل x-idempotency-key — التكرار يعيد نفس الاستجابة بلا تنفيذ ثانٍ. */
  idempotent?: boolean;
};

function json(status: number, body: unknown, rid: string): NextResponse {
  const res = NextResponse.json(body, { status });
  res.headers.set('x-request-id', rid);
  res.headers.set('Cache-Control', 'no-store');
  return res;
}

/** Wraps a handler with: session verify → fresh RBAC → CSRF origin → rate limit → envelope. */
export function withApi(opts: Opts, handler: (ctx: Ctx) => Promise<ApiResult<unknown> | Response>) {
  // `any` here: Next 15.5's route type validator expects its generated RouteContext
  // shape; a looser parameter keeps every route export assignable while the runtime
  // code below normalizes missing segments.
  return async (req: NextRequest, seg?: any): Promise<NextResponse> => {
    const routeSeg: { params: Promise<Record<string, string>> } =
      seg ?? { params: Promise.resolve({}) };
    const rid = nodeCrypto.randomUUID();
    try {
      const method = req.method.toUpperCase();
      const mutating = opts.mutating ?? ['POST', 'PATCH', 'PUT', 'DELETE'].includes(method);
      const payload: SessionPayload | null = verifySession(req.cookies.get(SESSION_COOKIE)?.value);
      if (!payload) return json(401, { ok: false, error: 'unauthenticated', code: rid }, rid);

      let role = payload.role;
      if (opts.minRole || mutating) {
        const row = await freshRoleRow(payload.sub);
        if (!row || !row.enabled) return json(403, { ok: false, error: 'not_authorized', code: rid }, rid);
        role = row.role;
        if (opts.minRole && !hasMin(role, opts.minRole)) {
          return json(403, { ok: false, error: 'forbidden', code: rid }, rid);
        }
      }
      if (mutating && !sameOrigin(req)) {
        return json(403, { ok: false, error: 'csrf_rejected', code: rid }, rid);
      }
      // فصل البيئات: نشر المعاينة (VERCEL_ENV=preview) للقراءة افتراضياً —
      // أي mutation مرفوض إلا إذا ضُبط PREVIEW_ALLOW_MUTATIONS=1 على بيئة المعاينة تحديداً.
      if (
        mutating &&
        process.env.VERCEL_ENV === 'preview' &&
        process.env.PREVIEW_ALLOW_MUTATIONS !== '1'
      ) {
        return json(403, { ok: false, error: 'preview_readonly: بيئة المعاينة للقراءة فقط — الإنتاج محمي', code: rid }, rid);
      }
      if (opts.rate && !rateLimit(`${opts.name}:${clientIp(req)}`, opts.rate.limit, opts.rate.windowMs)) {
        return json(429, { ok: false, error: 'rate_limited', code: rid }, rid);
      }
      // عدم التكرار: مفتاح صالح + عملية معلمة idempotent → فحص المخزن قبل التنفيذ
      const idemKey = opts.idempotent ? idemKeyFrom(req) : null;
      if (idemKey) {
        const hit = await idemGet(opts.name, idemKey);
        if (hit) {
          const rep = json(hit.status, hit.body, rid);
          rep.headers.set('x-idempotent-replay', 'true');
          return rep;
        }
      }
      let body: unknown = undefined;
      if (mutating) {
        try {
          body = await req.json();
        } catch {
          body = {};
        }
      }
      const res = await handler({
        req,
        email: payload.sub,
        role,
        body,
        rid,
        seg: routeSeg,
      });
      if (res instanceof Response) {
        res.headers.set('x-request-id', rid);
        return res as NextResponse;
      }
      const status = res.ok ? 200 : (res.status ?? 400);
      const out = json(status, res.ok ? res : { ok: false, error: res.error, code: rid }, rid);
      if (idemKey && status >= 200 && status < 300) {
        // نخزن النجاحات فقط — الفشل قابل لإعادة المحاولة بنفس المفتاح
        await idemSet(opts.name, idemKey, { status, body: res.ok ? res : { ok: false, error: res.error, code: rid } });
      }
      return out;
    } catch (e) {
      logger.error({ err: String(e), route: opts.name, rid }, 'api_error');
      return json(500, { ok: false, error: 'internal_error', code: rid }, rid);
    }
  };
}

/** Webhook wrapper: shared-secret header instead of session. */
export function withWebhook(
  name: string,
  handler: (ctx: { req: NextRequest; body: unknown; rid: string }) => Promise<ApiResult<unknown>>,
) {
  return async (req: NextRequest): Promise<NextResponse> => {
    const rid = nodeCrypto.randomUUID();
    try {
      const secret = process.env.WEBHOOK_SECRET || '';
      const given = req.headers.get('x-br7-webhook-secret') || '';
      if (!secret || given.length !== secret.length || !nodeCrypto.timingSafeEqual(Buffer.from(given), Buffer.from(secret))) {
        return json(401, { ok: false, error: 'invalid_webhook_secret', code: rid }, rid);
      }
      let body: unknown = {};
      try {
        body = await req.json();
      } catch {
        body = {};
      }
      const res = await handler({ req, body, rid });
      const status = res.ok ? 200 : (res.status ?? 400);
      return json(status, res, rid);
    } catch (e) {
      logger.error({ err: String(e), route: name, rid }, 'webhook_error');
      return json(500, { ok: false, error: 'internal_error', code: rid }, rid);
    }
  };
}

export function parseSearch(req: NextRequest) {
  const sp = req.nextUrl.searchParams;
  return {
    limit: Math.min(Math.max(parseInt(sp.get('limit') || '50', 10) || 50, 1), 200),
    offset: Math.max(parseInt(sp.get('offset') || '0', 10) || 0, 0),
    order: sp.get('order') || undefined,
    desc: sp.get('desc') === '1',
    filter: sp.get('filter') || undefined,
    search: sp.get('search') || undefined,
    format: sp.get('format') || undefined,
  };
}

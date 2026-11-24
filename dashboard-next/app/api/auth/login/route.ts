import { NextRequest, NextResponse } from 'next/server';
import { z } from 'zod';
import { signInWithPassword, requireService } from '@/lib/supabase';
import { signSession, SESSION_COOKIE, sessionCookieOptions } from '@/lib/session';
import { rateLimit } from '@/lib/rate-limit';
import { sameOrigin } from '@/lib/api';
import { logAudit } from '@/lib/audit';
import { logger } from '@/lib/logger';

export const dynamic = 'force-dynamic';

const Body = z
  .object({ email: z.string().email().max(200), password: z.string().min(8).max(128) })
  .strict();

export async function POST(req: NextRequest) {
  const rid = crypto.randomUUID();
  const ip = (req.headers.get('x-forwarded-for') || 'local').split(',')[0].trim();
  const j = (status: number, body: Record<string, unknown>) =>
    NextResponse.json(body, { status, headers: { 'x-request-id': rid, 'Cache-Control': 'no-store' } });

  try {
    if (!sameOrigin(req)) return j(403, { ok: false, error: 'csrf_rejected', code: rid });
    if (!rateLimit(`login:${ip}`, 5, 5 * 60000)) return j(429, { ok: false, error: 'rate_limited', code: rid });

    let raw: unknown = {};
    try {
      raw = await req.json();
    } catch {
      /* empty */
    }
    const parsed = Body.safeParse(raw);
    if (!parsed.success) return j(400, { ok: false, error: 'invalid_body', code: rid });

    const { email, password } = parsed.data;
    const login = await signInWithPassword(email, password);
    if (!login.ok) {
      await logAudit({
        actor: email, action: 'dashboard_login_failed', objectType: 'auth',
        source: 'vercel-dashboard', ip, userAgent: req.headers.get('user-agent') || '', requestId: rid,
      });
      return j(401, { ok: false, error: 'invalid_credentials', code: rid });
    }

    const sb = requireService();
    const { data: userRow } = await sb
      .from('dashboard_users')
      .select('role,enabled')
      .eq('username', email)
      .maybeSingle();
    if (!userRow || Number(userRow.enabled) !== 1) {
      await logAudit({
        actor: email, action: 'dashboard_login_rejected', objectType: 'auth',
        source: 'vercel-dashboard', ip, userAgent: req.headers.get('user-agent') || '', requestId: rid,
      });
      return j(403, { ok: false, error: 'not_authorized', code: rid });
    }

    const role = String(userRow.role || 'viewer');
    await sb.from('dashboard_users').update({ last_login_at: new Date().toISOString() }).eq('username', email);
    await logAudit({
      actor: email, action: 'dashboard_login_success', objectType: 'auth',
      source: 'vercel-dashboard', ip, userAgent: req.headers.get('user-agent') || '', requestId: rid,
    });

    const res = NextResponse.json({ ok: true, data: { email, role } });
    res.cookies.set(SESSION_COOKIE, signSession({ sub: email, role }), sessionCookieOptions());
    res.headers.set('x-request-id', rid);
    logger.info({ email, rid }, 'dashboard_login_ok');
    return res;
  } catch (e) {
    logger.error({ err: String(e), rid }, 'login_error');
    return j(500, { ok: false, error: 'internal_error', code: rid });
  }
}

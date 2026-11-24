import { NextRequest, NextResponse } from 'next/server';
import { verifySession, SESSION_COOKIE } from '@/lib/session';
import { requireService } from '@/lib/supabase';

export const dynamic = 'force-dynamic';

export async function GET(req: NextRequest) {
  const rid = crypto.randomUUID();
  const payload = verifySession(req.cookies.get(SESSION_COOKIE)?.value);
  if (!payload) {
    return NextResponse.json({ ok: false, error: 'unauthenticated' }, { status: 401, headers: { 'x-request-id': rid } });
  }
  let role = payload.role;
  let enabled = true;
  try {
    const sb = requireService();
    const { data } = await sb.from('dashboard_users').select('role,enabled').eq('username', payload.sub).maybeSingle();
    if (data) {
      role = String(data.role || role);
      enabled = Number(data.enabled) === 1;
    }
  } catch {
    /* fall back to token role */
  }
  return NextResponse.json({ ok: true, data: { email: payload.sub, role, enabled, exp: payload.exp } }, { headers: { 'x-request-id': rid } });
}

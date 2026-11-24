import { NextRequest, NextResponse } from 'next/server';
import { SESSION_COOKIE } from '@/lib/session';
import { logAudit } from '@/lib/audit';

export const dynamic = 'force-dynamic';

export async function POST(req: NextRequest) {
  const rid = crypto.randomUUID();
  const res = NextResponse.json({ ok: true, data: { loggedOut: true } });
  res.cookies.set(SESSION_COOKIE, '', { httpOnly: true, secure: true, sameSite: 'strict', path: '/', maxAge: 0 });
  res.headers.set('x-request-id', rid);
  const token = req.cookies.get(SESSION_COOKIE)?.value || '';
  const email = token.split('.')[0] ? (() => { try { return (JSON.parse(Buffer.from(token.split('.')[0], 'base64url').toString()) as { sub?: string }).sub || 'unknown'; } catch { return 'unknown'; } })() : 'unknown';
  await logAudit({ actor: email, action: 'dashboard_logout', objectType: 'auth', source: 'vercel-dashboard', requestId: rid });
  return res;
}

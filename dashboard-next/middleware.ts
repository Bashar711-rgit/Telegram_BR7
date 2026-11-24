import { NextRequest, NextResponse } from 'next/server';

/**
 * UX gate only: middleware checks cookie presence and redirects to /login.
 * Full HMAC + expiry verification happens server-side (lib/session.ts) in
 * layouts and API routes — a forged cookie never gets past those.
 */
export function middleware(req: NextRequest) {
  const token = req.cookies.get('br7_session')?.value;
  if (!token) {
    const url = req.nextUrl.clone();
    url.pathname = '/login';
    url.search = '';
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico|login|robots.txt).*)'],
};

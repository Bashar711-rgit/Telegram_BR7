import { NextRequest } from 'next/server';
import { verifySession, SESSION_COOKIE } from '@/lib/session';
import { hasMin } from '@/lib/auth';
import { freshRole } from '@/lib/api';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
// نافذة بث قصيرة ثم يعيد EventSource الاتصال تلقائياً — آمن على serverless
export const maxDuration = 60;

/**
 * GET /api/bot/stream — بث حي (SSE) لحالة المحرك (viewer+).
 * يدفع getBotStatus كل 10 ثوانٍ. الاتصال ينغلق بعد ~55 ثانية
 * ويعيد EventSource الاتصال فوراً — آمن ضمن حدود serverless.
 * بيانات الحالة مشتقة لحظياً من /health + /health/full + Render API — بلا تخزين قديم.
 */
export async function GET(req: NextRequest) {
  const payload = verifySession(req.cookies.get(SESSION_COOKIE)?.value);
  if (!payload) {
    return new Response(JSON.stringify({ ok: false, error: 'unauthenticated' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    });
  }
  const row = await freshRole(payload.sub);
  if (!row || !row.enabled || !hasMin(row.role, 'viewer')) {
    return new Response(JSON.stringify({ ok: false, error: 'forbidden' }), {
      status: 403,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  const { getBotStatus } = await import('@/lib/bot/engine');
  const encoder = new TextEncoder();
  let closed = false;

  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (event: string, data: unknown) => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
        } catch {
          closed = true;
        }
      };
      const t0 = Date.now();
      try {
        while (!closed && !req.signal.aborted && Date.now() - t0 < 55_000) {
          const st = await getBotStatus();
          send('status', st);
          if (closed) break;
          await new Promise((r) => setTimeout(r, 10_000));
        }
      } catch {
        // أي فشل في الاشتقاق → نبضة خطأ صادقة ثم إغلاق (العميل يعيد الاتصال)
        send('error', { at: new Date().toISOString() });
      }
      if (!closed) {
        try {
          controller.close();
        } catch {
          // أُغلق مسبقاً
        }
      }
    },
    cancel() {
      closed = true;
    },
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-store, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  });
}

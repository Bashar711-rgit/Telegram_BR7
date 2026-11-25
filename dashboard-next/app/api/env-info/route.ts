import { NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';

/**
 * GET /api/env-info — بيئة النشر للواجهة (بلا أسرار).
 * production → الإنتاج الكامل · preview → معاينة للقراءة فقط (حارس withApi)
 * · development → محلي. يغذي شارة البيئة في الهيكل.
 */
export function GET() {
  const vercelEnv = String(process.env.VERCEL_ENV || 'development');
  return NextResponse.json(
    {
      env: vercelEnv,
      isProduction: vercelEnv === 'production',
      isPreview: vercelEnv === 'preview',
      previewMutationsAllowed: process.env.PREVIEW_ALLOW_MUTATIONS === '1',
    },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}

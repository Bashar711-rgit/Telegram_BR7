'use client';

import { createBrowserClient } from '@supabase/ssr';

/**
 * Browser client — ANON key only, by design.
 * Every privileged operation (service role) stays server-side in lib/supabase.ts;
 * the service key is never inlined into the client bundle.
 */
export function getSupabaseBrowser() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const anon = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !anon) return null;
  return createBrowserClient(url, anon);
}

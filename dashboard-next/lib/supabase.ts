import { createClient, SupabaseClient } from '@supabase/supabase-js';

export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL || '';
export const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
export const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || '';

function makeClient(key: string): SupabaseClient {
  return createClient(SUPABASE_URL, key, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: { headers: { 'X-Client-Info': 'br7-control-plane' } },
  });
}

/**
 * Server-side service client — SERVICE_ROLE ONLY, server environment.
 * Never imported from client components; the service key never reaches the browser.
 */
export const serviceClient: SupabaseClient | null =
  SUPABASE_URL && SUPABASE_SERVICE_KEY ? makeClient(SUPABASE_SERVICE_KEY) : null;

export function requireService(): SupabaseClient {
  if (!serviceClient) throw new Error('supabase_not_configured');
  return serviceClient;
}

/**
 * Verify email+password against Supabase Auth. Uses the ANON key server-side —
 * the session itself is minted by our own HttpOnly signed cookie (lib/session.ts).
 */
export async function signInWithPassword(email: string, password: string): Promise<{ ok: boolean; error?: string }> {
  if (!SUPABASE_URL || !SUPABASE_ANON_KEY) return { ok: false, error: 'supabase_not_configured' };
  try {
    const anon = makeClient(SUPABASE_ANON_KEY);
    const { error } = await anon.auth.signInWithPassword({ email, password });
    if (error) return { ok: false, error: 'invalid_credentials' };
    return { ok: true };
  } catch {
    return { ok: false, error: 'invalid_credentials' };
  }
}

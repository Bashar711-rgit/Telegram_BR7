/**
 * fetchJson — every external call goes through this:
 * - hard timeout (default 15s)
 * - optional single retry (caller decides; never retry non-idempotent POSTs)
 * Returns {status, data, error?} — never throws.
 */
export async function fetchJson<T = unknown>(
  url: string,
  init: RequestInit & { timeoutMs?: number; retries?: number } = {},
): Promise<{ status: number; data: T | null; error?: string }> {
  const { timeoutMs = 15000, retries = 0, ...rest } = init;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...rest, signal: ctrl.signal });
      clearTimeout(t);
      const text = await res.text();
      let data: unknown = null;
      try {
        data = text ? JSON.parse(text) : null;
      } catch {
        data = text;
      }
      return { status: res.status, data: data as T };
    } catch (e) {
      clearTimeout(t);
      lastErr = e;
      if (attempt >= retries) break;
      await new Promise((r) => setTimeout(r, 400));
    }
  }
  return { status: 0, data: null, error: String(lastErr) };
}

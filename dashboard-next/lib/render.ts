import { fetchJson } from './http';

const BASE = 'https://api.render.com/v1';
const RENDER_URL = () => process.env.RENDER_URL || 'https://telegram-br7.onrender.com';

function authHeaders(): Record<string, string> {
  return {
    Authorization: `Bearer ${process.env.RENDER_API_KEY || ''}`,
    Accept: 'application/json',
  };
}

export type ExternalResult<T> = { status: number; data: T | null; error?: string };

function configured(): boolean {
  return Boolean(process.env.RENDER_API_KEY && process.env.RENDER_SERVICE_ID);
}

async function call<T>(path: string, init: RequestInit = {}, retries = 0): Promise<ExternalResult<T>> {
  if (!configured()) return { status: 0, data: null, error: 'render_not_configured' };
  const r = await fetchJson<T>(`${BASE}${path}`, {
    ...init,
    headers: { ...authHeaders(), ...((init.headers as Record<string, string>) || {}) },
    timeoutMs: 15000,
    retries,
  });
  return r;
}

export function renderPublicUrl(): string {
  return RENDER_URL();
}

export async function getService(): Promise<ExternalResult<Record<string, unknown>>> {
  return call(`/services/${process.env.RENDER_SERVICE_ID}`, {}, 1);
}

export async function listDeploys(limit = 10): Promise<ExternalResult<Array<Record<string, unknown>>>> {
  return call(`/services/${process.env.RENDER_SERVICE_ID}/deploys?limit=${limit}`, {}, 1);
}

export async function getDeploy(deployId: string): Promise<ExternalResult<Record<string, unknown>>> {
  return call(`/services/${process.env.RENDER_SERVICE_ID}/deploys/${deployId}`, {}, 1);
}

export async function listEnvVars(): Promise<ExternalResult<Array<{ envVar: { key: string; value: string } }>>> {
  return call(`/services/${process.env.RENDER_SERVICE_ID}/env-vars`, {}, 1);
}

export async function upsertEnvVar(key: string, value: string): Promise<ExternalResult<unknown>> {
  return call(
    `/services/${process.env.RENDER_SERVICE_ID}/env-vars/${encodeURIComponent(key)}`,
    { method: 'PUT', body: JSON.stringify({ value }), headers: { 'Content-Type': 'application/json' } },
  );
}

export async function deleteEnvVar(key: string): Promise<ExternalResult<unknown>> {
  return call(`/services/${process.env.RENDER_SERVICE_ID}/env-vars/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export async function triggerDeploy(clearCache = 'do_not_clear'): Promise<ExternalResult<{ id: string }>> {
  return call(
    `/services/${process.env.RENDER_SERVICE_ID}/deploys`,
    { method: 'POST', body: JSON.stringify({ clearCache }), headers: { 'Content-Type': 'application/json' } },
  );
}

/**
 * Restart: prefer the dedicated endpoint; if the API version lacks it,
 * fall back to a cache-preserving deploy (same operational effect).
 */
export async function restartService(): Promise<ExternalResult<unknown> & { via?: string }> {
  const direct = await call(`/services/${process.env.RENDER_SERVICE_ID}/restart`, { method: 'POST' });
  if (direct.status >= 200 && direct.status < 300) return { ...direct, via: 'restart_endpoint' };
  const fallback = await triggerDeploy();
  return { ...fallback, via: 'deploy_fallback' };
}

export async function botHealth(): Promise<{ reachable: boolean; status?: string; dbHealthy?: boolean }> {
  const r = await fetchJson<{ status?: string; db_healthy?: boolean }>(`${RENDER_URL()}/health`, { timeoutMs: 8000 });
  if (r.status !== 200 || !r.data) return { reachable: false };
  return { reachable: true, status: r.data.status, dbHealthy: Boolean(r.data.db_healthy) };
}

/** Env keys that must never be deleted or blindly overwritten. */
export const PROTECTED_ENV_RE = /^(DATABASE_URL|SECRET_KEY|SESSION_.*)$/i;

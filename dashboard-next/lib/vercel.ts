import { fetchJson } from './http';

export type ExternalResult<T> = { status: number; data: T | null; error?: string };

const BASE = 'https://api.vercel.com';
const PROJECT_NAME = 'telegram-br-7';

function tok(): string {
  return process.env.VERCEL_TOKEN || '';
}
function pid(): string {
  return process.env.VERCEL_PROJECT_ID || '';
}

function configured(): boolean {
  return Boolean(tok() && pid());
}

async function call<T>(path: string, init: RequestInit = {}, retries = 0): Promise<ExternalResult<T>> {
  if (!configured()) return { status: 0, data: null, error: 'vercel_not_configured' };
  const r = await fetchJson<T>(`${BASE}${path}`, {
    ...init,
    headers: { Authorization: `Bearer ${tok()}`, ...((init.headers as Record<string, string>) || {}) },
    timeoutMs: 15000,
    retries,
  });
  return r;
}

export async function listDeployments(limit = 8): Promise<ExternalResult<Array<Record<string, unknown>>>> {
  return call(`/v6/deployments?project=${pid()}&limit=${limit}`, {}, 1);
}

export async function getDeployment(id: string): Promise<ExternalResult<Record<string, unknown>>> {
  return call(`/v13/deployments/${id}`);
}

/** Rebuild the latest preview of the working branch. Production is intentionally out of reach here. */
export async function redeployPreview(deploymentId?: string): Promise<ExternalResult<Record<string, unknown>>> {
  const body: Record<string, unknown> = { name: PROJECT_NAME, target: 'preview' };
  if (deploymentId) body.deploymentId = deploymentId;
  return call('/v13/deployments', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
}

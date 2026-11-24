import { Octokit } from '@octokit/rest';

export type ExternalResult<T> = { status: number; data: T | null; error?: string };

function gh(): Octokit | null {
  const token = process.env.GITHUB_TOKEN;
  if (!token) return null;
  return new Octokit({ auth: token, request: { timeout: 15000 } });
}

function coords(): { owner: string; repo: string } {
  return {
    owner: process.env.GITHUB_OWNER || 'Bashar711-rgit',
    repo: process.env.GITHUB_REPO || 'Telegram_BR7',
  };
}

function fail(e: unknown): { status: number; data: null; error: string } {
  const status = (e as { status?: number }).status ?? 0;
  const message = String((e as { message?: string }).message || e);
  return { status, data: null, error: message };
}

export async function listReleases(perPage = 10): Promise<ExternalResult<Array<Record<string, unknown>>>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.repos.listReleases({ ...coords(), per_page: perPage });
    return { status: r.status, data: r.data as unknown as Array<Record<string, unknown>> };
  } catch (e) {
    return fail(e);
  }
}

export async function listRuns(perPage = 10): Promise<ExternalResult<Record<string, unknown>>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.actions.listWorkflowRunsForRepo({ ...coords(), per_page: perPage });
    return { status: r.status, data: r.data as unknown as Record<string, unknown> };
  } catch (e) {
    return fail(e);
  }
}

export async function rerunRun(runId: number): Promise<ExternalResult<null>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.actions.reRunWorkflow({ ...coords(), run_id: runId });
    return { status: r.status, data: null };
  } catch (e) {
    return fail(e);
  }
}

export async function listCommits(perPage = 10): Promise<ExternalResult<Array<Record<string, unknown>>>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.repos.listCommits({ ...coords(), per_page: perPage });
    return { status: r.status, data: r.data as unknown as Array<Record<string, unknown>> };
  } catch (e) {
    return fail(e);
  }
}

/** Secret NAMES only — values are never retrievable via API by design. */
export async function listSecretNames(): Promise<ExternalResult<Array<string>>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.actions.listRepoSecrets({ ...coords(), per_page: 100 });
    const names = (r.data.secrets || []).map((s) => s.name);
    return { status: r.status, data: names };
  } catch (e) {
    return fail(e);
  }
}

export async function getHeadSha(branch = 'main'): Promise<ExternalResult<string>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.repos.getBranch({ ...coords(), branch });
    const sha = r.data.commit?.sha;
    return typeof sha === 'string' ? { status: r.status, data: sha } : { status: 500, data: null, error: 'no_sha' };
  } catch (e) {
    return fail(e);
  }
}

export async function createTag(tag: string, sha?: string): Promise<ExternalResult<Record<string, unknown>>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  let targetSha = sha;
  if (!targetSha) {
    const head = await getHeadSha();
    if (!head.data) return { status: head.status, data: null, error: head.error || 'no_head_sha' };
    targetSha = head.data;
  }
  try {
    const r = await o.rest.git.createRef({ ...coords(), ref: `refs/tags/${tag}`, sha: targetSha });
    return { status: r.status, data: r.data as unknown as Record<string, unknown> };
  } catch (e) {
    return fail(e);
  }
}

export async function deleteTag(tag: string): Promise<ExternalResult<null>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.git.deleteRef({ ...coords(), ref: `refs/tags/${tag}` });
    return { status: r.status, data: null };
  } catch (e) {
    return fail(e);
  }
}

export async function repoInfo(): Promise<ExternalResult<Record<string, unknown>>> {
  const o = gh();
  if (!o) return { status: 0, data: null, error: 'github_not_configured' };
  try {
    const r = await o.rest.repos.get({ ...coords() });
    return { status: r.status, data: r.data as unknown as Record<string, unknown> };
  } catch (e) {
    return fail(e);
  }
}

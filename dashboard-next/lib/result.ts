export type ApiResult<T = unknown> =
  | { ok: true; data: T }
  | { ok: false; error: string; status?: number; code?: string };

export const ok = <T,>(data: T): ApiResult<T> => ({ ok: true, data });
export const err = (error: string, status = 400): ApiResult<never> => ({ ok: false, error, status });

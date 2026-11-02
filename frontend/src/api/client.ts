/**
 * API client.
 *
 * Security model: the short-lived access token lives only in memory (never in
 * localStorage, where any injected script could read it). The long-lived
 * refresh token is an httpOnly cookie the browser sends to /api/v1/auth/*.
 * On a 401 the client refreshes once and retries the request.
 */

export class ApiError extends Error {
  status: number;
  errors: { loc?: (string | number)[]; msg?: string; row?: number; error?: string }[];

  constructor(status: number, message: string, errors: ApiError["errors"] = []) {
    super(message);
    this.status = status;
    this.errors = errors;
  }
}

const BASE = "/api/v1";
let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
let onAuthLost: () => void = () => {};

export function setAccessToken(token: string | null) {
  accessToken = token;
}

export function onUnauthenticated(cb: () => void) {
  onAuthLost = cb;
}

async function parseError(res: Response): Promise<ApiError> {
  try {
    const body = await res.json();
    const detail = body.detail ?? body.title ?? res.statusText;
    return new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail), body.errors);
  } catch {
    return new ApiError(res.status, res.statusText || "Request failed");
  }
}

export async function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const res = await fetch(`${BASE}/auth/refresh`, { method: "POST", credentials: "same-origin" });
        if (!res.ok) return false;
        setAccessToken((await res.json()).access_token);
        return true;
      } catch {
        return false;
      } finally {
        // Callers already awaiting share this promise; later callers start a fresh refresh.
        refreshing = null;
      }
    })();
  }
  return refreshing;
}

type Options = { method?: string; body?: unknown; form?: FormData; raw?: boolean; retry?: boolean };

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const headers: Record<string, string> = {};
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  let body: BodyInit | undefined;
  if (opts.form) body = opts.form;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(`${BASE}${path}`, {
    method: opts.method ?? (body ? "POST" : "GET"),
    headers,
    body,
    credentials: "same-origin",
  });
  if (res.status === 401 && !path.startsWith("/auth/")) {
    if (opts.retry !== false && (await refreshSession())) return api<T>(path, { ...opts, retry: false });
    onAuthLost();
  }
  if (!res.ok) throw await parseError(res);
  if (opts.raw) return (await res.blob()) as T;
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export async function download(path: string, filename: string) {
  const blob = await api<Blob>(path, { raw: true });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

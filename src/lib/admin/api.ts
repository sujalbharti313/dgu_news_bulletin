/** Client for the Stage 3.5 editions and Stage 3.6 cleanup admin APIs. Every call carries the admin's Supabase
 * access token. */
import { API_BASE_URL } from "@/lib/api/config";
import type { AdminSession } from "./auth";
import { needsRefresh, refreshSession } from "./auth";
import { AdminApiError, errorFromResponse, FRIENDLY } from "./errors";
import type {
  Candidate,
  CleanupJob,
  CleanupJobDetail,
  CleanupPolicy,
  CleanupRunReport,
  EditionDetail,
  EditionSummary,
  GenerationReport,
  Issue,
} from "./types";

export { AdminApiError } from "./errors";

/** Keeps the current session fresh and lets the caller learn about refreshed or lost sessions. */
export interface SessionHandle {
  get(): AdminSession | null;
  set(session: AdminSession | null): void;
}

const EDITIONS = "/api/v1/admin/editions";
const CLEANUP = "/api/v1/admin/cleanup";

async function call<T>(
  handle: SessionHandle,
  method: "GET" | "POST" | "PUT",
  path: string,
  body?: unknown,
  retried = false,
  base: string = EDITIONS,
): Promise<T> {
  if (!API_BASE_URL) throw new AdminApiError(0, "not_configured", "NEXT_PUBLIC_API_BASE_URL is not set for this site.");
  let session = handle.get();
  if (!session) throw new AdminApiError(401, "signed_out", FRIENDLY[401]);
  if (needsRefresh(session, Date.now())) {
    session = await refreshSession(session).catch(() => null);
    handle.set(session);
    if (!session) throw new AdminApiError(401, "signed_out", FRIENDLY[401]);
  }
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${base}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${session.accessToken}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
    });
  } catch {
    throw new AdminApiError(0, "network", "Could not reach the AI//CAMPUS API. Check your connection and try again.");
  }
  if (res.status === 401 && !retried) {
    const fresh = await refreshSession(session).catch(() => null);
    handle.set(fresh);
    if (fresh) return call<T>(handle, method, path, body, true, base);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401) handle.set(null);
    throw errorFromResponse(res.status, data);
  }
  return data as T;
}

export interface SaveResult {
  changed: boolean;
  revision: number;
  status: string;
  contentHash?: string;
  issues?: Issue[];
}

export interface StatusResult {
  status: string;
  revision: number;
  alreadyApproved?: boolean;
  alreadyPublished?: boolean;
  slug?: string;
}

export function adminApi(handle: SessionHandle) {
  return {
    session: () => call<{ userId: string; email: string; name: string | null }>(handle, "GET", "/session"),
    list: () => call<{ items: EditionSummary[] }>(handle, "GET", "?limit=100"),
    generate: (week: string) => call<GenerationReport>(handle, "POST", "/generate", { week }),
    detail: (id: string) => call<EditionDetail>(handle, "GET", `/${encodeURIComponent(id)}`),
    revision: (id: string, n: number) => call<Record<string, unknown>>(handle, "GET", `/${encodeURIComponent(id)}/revisions/${n}`),
    candidates: (id: string) => call<{ items: Candidate[] }>(handle, "GET", `/${encodeURIComponent(id)}/candidates`),
    preview: (id: string) => call<Record<string, unknown>>(handle, "GET", `/${encodeURIComponent(id)}/preview`),
    save: (id: string, payload: unknown) => call<SaveResult>(handle, "PUT", `/${encodeURIComponent(id)}/content`, payload),
    submit: (id: string, expectedRevision: number) =>
      call<StatusResult>(handle, "POST", `/${encodeURIComponent(id)}/submit`, { expectedRevision }),
    approve: (id: string, expectedRevision: number, contentHash: string) =>
      call<StatusResult>(handle, "POST", `/${encodeURIComponent(id)}/approve`, { expectedRevision, contentHash }),
    reject: (id: string, expectedRevision: number, reason: string, returnForRevision: boolean) =>
      call<StatusResult>(handle, "POST", `/${encodeURIComponent(id)}/reject`, { expectedRevision, reason, returnForRevision }),
    publish: (id: string, expectedRevision: number) =>
      call<StatusResult>(handle, "POST", `/${encodeURIComponent(id)}/publish`, { expectedRevision }),
    regenerate: (id: string, expectedRevision: number) =>
      call<GenerationReport>(handle, "POST", `/${encodeURIComponent(id)}/regenerate`, { expectedRevision }),
    archive: (id: string, expectedRevision: number) =>
      call<StatusResult>(handle, "POST", `/${encodeURIComponent(id)}/archive`, { expectedRevision }),
    cleanup: {
      policy: () => call<CleanupPolicy>(handle, "GET", "/policy", undefined, false, CLEANUP),
      jobs: () => call<{ items: CleanupJob[] }>(handle, "GET", "/jobs?limit=100", undefined, false, CLEANUP),
      job: (id: string, state?: string) =>
        call<CleanupJobDetail>(
          handle,
          "GET",
          `/jobs/${encodeURIComponent(id)}${state ? `?state=${encodeURIComponent(state)}` : ""}`,
          undefined,
          false,
          CLEANUP,
        ),
      retry: (id: string) => call<CleanupJob>(handle, "POST", `/jobs/${encodeURIComponent(id)}/retry`, undefined, false, CLEANUP),
      cancel: (id: string) => call<CleanupJob>(handle, "POST", `/jobs/${encodeURIComponent(id)}/cancel`, undefined, false, CLEANUP),
      reopen: (id: string) => call<CleanupJob>(handle, "POST", `/jobs/${encodeURIComponent(id)}/reopen`, undefined, false, CLEANUP),
      plan: (editionId: string) =>
        call<CleanupJob>(handle, "POST", `/editions/${encodeURIComponent(editionId)}/plan`, undefined, false, CLEANUP),
      run: () => call<CleanupRunReport>(handle, "POST", "/run", undefined, false, CLEANUP),
    },
  };
}

export type AdminApi = ReturnType<typeof adminApi>;

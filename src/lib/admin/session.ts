/** Pure session helpers (unit tested with `npm test`). */

export interface AdminSession {
  accessToken: string;
  refreshToken: string;
  /** Epoch milliseconds. */
  expiresAt: number;
  email: string;
}

const REFRESH_MARGIN_MS = 60_000;

/** Turns a Supabase token response into a session, or null when it is not a usable one. */
export function parseTokenResponse(body: unknown, now: number): AdminSession | null {
  if (!body || typeof body !== "object") return null;
  const b = body as Record<string, unknown>;
  const user = (b.user ?? {}) as Record<string, unknown>;
  if (typeof b.access_token !== "string" || typeof b.refresh_token !== "string") return null;
  const expiresIn = typeof b.expires_in === "number" && b.expires_in > 0 ? b.expires_in : 3600;
  return {
    accessToken: b.access_token,
    refreshToken: b.refresh_token,
    expiresAt: now + expiresIn * 1000,
    email: typeof user.email === "string" ? user.email : "",
  };
}

export function needsRefresh(session: AdminSession, now: number): boolean {
  return session.expiresAt - now < REFRESH_MARGIN_MS;
}


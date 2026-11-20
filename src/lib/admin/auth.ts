/**
 * Editorial admin sign-in with Supabase Auth (email + password) over its REST API.
 *
 * Only public values are used here: the project URL and the publishable key, which Supabase designs to be
 * shipped to browsers. Being signed in proves nothing by itself: the backend verifies every access token with
 * Supabase and then checks the `editorial_admins` allow-list. Tokens live in sessionStorage (cleared when the
 * tab closes) and are sent only as an Authorization header to the AI//CAMPUS API, never as cookies, so the
 * admin API is not exposed to CSRF.
 */

import { type AdminSession, parseTokenResponse } from "./session";

export type { AdminSession } from "./session";
export { needsRefresh } from "./session";

const STORAGE_KEY = "ai-campus-admin-session";

export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL?.replace(/\/$/, "") || null;
export const SUPABASE_PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY || null;
export const isAdminSignInConfigured = SUPABASE_URL !== null && SUPABASE_PUBLISHABLE_KEY !== null;

export class AuthError extends Error {}

export function loadSession(): AdminSession | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as AdminSession;
    return typeof s.accessToken === "string" && typeof s.refreshToken === "string" ? s : null;
  } catch {
    return null;
  }
}

export function storeSession(session: AdminSession | null): void {
  try {
    if (session) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable: the session lasts for this page only */
  }
}

async function tokenRequest(grant: "password" | "refresh_token", body: Record<string, string>): Promise<AdminSession> {
  if (!isAdminSignInConfigured) throw new AuthError("Admin sign-in is not configured for this site.");
  let res: Response;
  try {
    res = await fetch(`${SUPABASE_URL}/auth/v1/token?grant_type=${grant}`, {
      method: "POST",
      headers: { apikey: SUPABASE_PUBLISHABLE_KEY!, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      cache: "no-store",
    });
  } catch {
    throw new AuthError("Could not reach the sign-in service. Check your connection and try again.");
  }
  if (res.status === 400 || res.status === 401 || res.status === 403) {
    throw new AuthError(grant === "password" ? "Email or password is incorrect." : "Your sign-in has expired.");
  }
  if (res.status === 429) throw new AuthError("Too many sign-in attempts. Wait a few minutes and try again.");
  if (!res.ok) throw new AuthError("The sign-in service is unavailable right now.");
  const session = parseTokenResponse(await res.json().catch(() => null), Date.now());
  if (!session) throw new AuthError("The sign-in service returned an unexpected answer.");
  return session;
}

export async function signIn(email: string, password: string): Promise<AdminSession> {
  const session = await tokenRequest("password", { email: email.trim(), password });
  storeSession(session);
  return session;
}

export async function refreshSession(session: AdminSession): Promise<AdminSession> {
  try {
    const next = await tokenRequest("refresh_token", { refresh_token: session.refreshToken });
    const merged = { ...next, email: next.email || session.email };
    storeSession(merged);
    return merged;
  } catch (err) {
    storeSession(null);
    throw err;
  }
}

/** Revokes the refresh token at Supabase (best effort) and forgets the session locally. */
export async function signOut(session: AdminSession | null): Promise<void> {
  storeSession(null);
  if (!session || !isAdminSignInConfigured) return;
  try {
    await fetch(`${SUPABASE_URL}/auth/v1/logout`, {
      method: "POST",
      headers: { apikey: SUPABASE_PUBLISHABLE_KEY!, Authorization: `Bearer ${session.accessToken}` },
      cache: "no-store",
    });
  } catch {
    /* already forgotten locally */
  }
}

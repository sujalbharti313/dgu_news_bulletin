"use client";

import { Loader2, LogOut, ShieldCheck } from "lucide-react";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { adminApi, AdminApiError, type AdminApi } from "@/lib/admin/api";
import { type AdminSession, AuthError, isAdminSignInConfigured, loadSession, signIn, signOut } from "@/lib/admin/auth";
import { API_BASE_URL } from "@/lib/api/config";

interface AdminContextValue {
  api: AdminApi;
  email: string;
  name: string | null;
  signOut: () => void;
}

const AdminContext = createContext<AdminContextValue | null>(null);

export function useAdmin(): AdminContextValue {
  const ctx = useContext(AdminContext);
  if (!ctx) throw new Error("useAdmin must be used inside <AdminShell>");
  return ctx;
}

type Phase = "checking" | "signed_out" | "denied" | "ready";

/** Signs the editor in with Supabase Auth and confirms with the backend that the account is an editorial admin. */
export function AdminShell({ children }: { children: React.ReactNode }) {
  const sessionRef = useRef<AdminSession | null>(null);
  const [phase, setPhase] = useState<Phase>("checking");
  const [me, setMe] = useState<{ email: string; name: string | null } | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const handle = useMemo(
    () => ({
      get: () => sessionRef.current,
      set: (s: AdminSession | null) => {
        sessionRef.current = s;
        if (!s) setPhase("signed_out");
      },
    }),
    [],
  );
  const api = useMemo(() => adminApi(handle), [handle]);

  const confirm = useCallback(async () => {
    try {
      const who = await api.session();
      setMe({ email: who.email, name: who.name });
      setPhase("ready");
    } catch (err) {
      if (err instanceof AdminApiError && err.status === 403) {
        setPhase("denied");
      } else {
        setMessage(err instanceof Error ? err.message : "Could not check your account.");
        setPhase(sessionRef.current ? "denied" : "signed_out");
      }
    }
  }, [api]);

  useEffect(() => {
    sessionRef.current = loadSession();
    if (sessionRef.current) void confirm();
    else setPhase("signed_out");
  }, [confirm]);

  const doSignOut = useCallback(() => {
    void signOut(sessionRef.current);
    sessionRef.current = null;
    setMe(null);
    setPhase("signed_out");
  }, []);

  if (!isAdminSignInConfigured || !API_BASE_URL) {
    return (
      <AdminFrame>
        <Panel title="Admin sign-in is not configured">
          <p>
            Set <code>NEXT_PUBLIC_API_BASE_URL</code>, <code>NEXT_PUBLIC_SUPABASE_URL</code> and{" "}
            <code>NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY</code> for this site, and <code>SUPABASE_URL</code> /{" "}
            <code>SUPABASE_PUBLISHABLE_KEY</code> for the backend. See the backend README, “Weekly editions”.
          </p>
        </Panel>
      </AdminFrame>
    );
  }

  if (phase === "checking") {
    return (
      <AdminFrame>
        <p role="status" className="flex items-center gap-3 text-ink-muted">
          <Loader2 className="size-5 animate-spin text-purple" aria-hidden /> Checking your sign-in…
        </p>
      </AdminFrame>
    );
  }

  if (phase === "signed_out") {
    return (
      <AdminFrame>
        <SignInForm
          notice={message}
          onSignedIn={(s) => {
            sessionRef.current = s;
            setMessage(null);
            setPhase("checking");
            void confirm();
          }}
        />
      </AdminFrame>
    );
  }

  if (phase === "denied") {
    return (
      <AdminFrame>
        <Panel title="No editorial access">
          <p>{message ?? "This account is signed in but is not an active editorial admin of AI//CAMPUS."}</p>
          <p className="mt-2">
            An existing admin can add it from the backend with{" "}
            <code>python -m scripts.edition admins add your@email</code>.
          </p>
          <button type="button" onClick={doSignOut} className="btn-secondary mt-6">
            Sign out
          </button>
        </Panel>
      </AdminFrame>
    );
  }

  return (
    <AdminContext.Provider value={{ api, email: me?.email ?? "", name: me?.name ?? null, signOut: doSignOut }}>
      <AdminFrame
        user={
          <div className="flex items-center gap-3 text-sm text-ink-muted">
            <span className="hidden sm:inline">{me?.name || me?.email}</span>
            <button type="button" onClick={doSignOut} className="btn-secondary !px-3 !py-1.5">
              <LogOut className="size-4" aria-hidden /> Sign out
            </button>
          </div>
        }
      >
        {children}
      </AdminFrame>
    </AdminContext.Provider>
  );
}

function AdminFrame({ children, user }: { children: React.ReactNode; user?: React.ReactNode }) {
  return (
    <main id="main" className="min-h-screen bg-soft pb-24 pt-28 sm:pt-32">
      <div className="shell">
        <div className="mb-10 flex flex-wrap items-center justify-between gap-4 border-b border-line pb-5">
          <p className="eyebrow flex items-center gap-2 text-purple">
            <ShieldCheck className="size-4" aria-hidden /> Editorial admin
          </p>
          {user}
        </div>
        {children}
      </div>
    </main>
  );
}

export function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="max-w-2xl rounded-[var(--radius-md)] border border-line bg-white p-6 text-ink sm:p-8">
      <h1 className="display text-3xl">{title}</h1>
      <div className="mt-4 leading-relaxed text-ink-muted">{children}</div>
    </section>
  );
}

function SignInForm({ onSignedIn, notice }: { onSignedIn: (s: AdminSession) => void; notice: string | null }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await signIn(email, password));
      setPassword("");
    } catch (err) {
      setError(err instanceof AuthError ? err.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="max-w-md rounded-[var(--radius-md)] border border-line bg-white p-6 sm:p-8" aria-labelledby="signin-title">
      <h1 id="signin-title" className="display text-3xl text-ink">
        Sign in
      </h1>
      <p className="mt-3 text-sm text-ink-muted">For AI//CAMPUS editors only. Students do not need an account.</p>
      {notice && <p className="mt-4 rounded-lg bg-purple/[0.06] px-3 py-2 text-sm text-ink">{notice}</p>}
      <label className="field-label mt-6" htmlFor="admin-email">
        Email
      </label>
      <input
        id="admin-email"
        type="email"
        autoComplete="username"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        className="field"
      />
      <label className="field-label mt-4" htmlFor="admin-password">
        Password
      </label>
      <input
        id="admin-password"
        type="password"
        autoComplete="current-password"
        required
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        className="field"
      />
      {error && (
        <p role="alert" className="mt-4 text-sm font-medium text-purple">
          {error}
        </p>
      )}
      <button type="submit" disabled={busy} className="btn-primary mt-6 w-full justify-center">
        {busy && <Loader2 className="size-4 animate-spin" aria-hidden />} Sign in
      </button>
    </form>
  );
}

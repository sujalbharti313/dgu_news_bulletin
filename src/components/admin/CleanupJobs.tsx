"use client";

import { ArrowLeft, ChevronDown, Loader2, Play, RefreshCw, RotateCcw, ShieldCheck, XCircle } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { canCancel, cleanupStatusLabel, reasonLabel, relativeTime } from "@/lib/admin/cleanup";
import { weekLabel } from "@/lib/admin/editor";
import type { CleanupJob, CleanupJobDetail, CleanupPolicy } from "@/lib/admin/types";
import { useAdmin } from "./AdminShell";

const TONES: Record<string, string> = {
  scheduled: "bg-purple/10 text-purple",
  staged: "bg-yellow text-ink",
  running: "bg-purple text-white",
  completed: "bg-ink text-white",
  blocked: "border border-purple text-purple",
  failed: "border border-purple text-purple",
  cancelled: "bg-ink/5 text-ink-muted",
};

function Badge({ status }: { status: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${TONES[status] ?? "bg-ink/5 text-ink"}`}>
      {cleanupStatusLabel(status)}
    </span>
  );
}

function when(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const local = new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", dateStyle: "medium", timeStyle: "short" }).format(d);
  return `${local} IST (${relativeTime(iso, Date.now())})`;
}

/** Stage 3.6: what the automatic cleanup has planned, kept and deleted. Read-mostly; every action is re-checked
 * by the backend, and none of them can delete a published edition or bypass a safety delay. */
export function CleanupJobs() {
  const { api } = useAdmin();
  const [jobs, setJobs] = useState<CleanupJob[] | null>(null);
  const [policy, setPolicy] = useState<CleanupPolicy | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<CleanupJobDetail | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [j, p] = await Promise.all([api.cleanup.jobs(), api.cleanup.policy()]);
      setJobs(j.items);
      setPolicy(p);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load cleanup jobs.");
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggle(id: string) {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    setDetail(null);
    try {
      setDetail(await api.cleanup.job(id));
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not load the job.");
    }
  }

  async function act(label: string, fn: () => Promise<unknown>, done: string) {
    setBusy(label);
    setNotice(null);
    try {
      await fn();
      setNotice(done);
      await load();
      if (open) setDetail(await api.cleanup.job(open));
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "The action failed.");
    } finally {
      setBusy(null);
    }
  }

  async function runNow() {
    setBusy("run");
    setNotice(null);
    try {
      const r = await api.cleanup.run();
      setNotice(r.deferred ?? r.message);
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "The cleanup run failed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <Link href="/admin" className="on-light inline-flex items-center gap-2 text-sm font-semibold text-purple">
        <ArrowLeft className="size-4" aria-hidden /> Weekly editions
      </Link>
      <div className="mt-4 flex flex-wrap items-end justify-between gap-6">
        <div>
          <h1 className="display text-[clamp(2.2rem,5vw,3.6rem)] text-ink">Data cleanup</h1>
          <p className="mt-3 max-w-2xl text-ink-muted">
            After a week’s edition is published and the safety delays have passed, the stories that were not selected are
            removed automatically. Published editions, their stories, source links and the audit history are never deleted.
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <button type="button" className="btn-secondary" onClick={() => void load()}>
            <RefreshCw className="size-4" aria-hidden /> Refresh
          </button>
          <button type="button" className="btn-primary" onClick={() => void runNow()} disabled={busy !== null}>
            {busy === "run" ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Play className="size-4" aria-hidden />}
            Run due cleanup now
          </button>
        </div>
      </div>

      {policy && (
        <section className="mt-8 rounded-[var(--radius-md)] border border-line bg-white p-5 text-sm text-ink-muted">
          <p className="flex items-center gap-2 font-semibold text-ink">
            <ShieldCheck className="size-4 text-purple" aria-hidden /> Retention policy {policy.version}
            {!policy.enabled && <span className="text-purple">(cleanup is switched off)</span>}
          </p>
          <ul className="mt-3 list-disc space-y-1 pl-5">
            <li>
              Unselected stories of a published week: deleted after the later of publication +{" "}
              {policy.publishedWeek.safety_delay_hours} hours and the week’s end + {policy.publishedWeek.min_story_age_days} days,
              then a {policy.publishedWeek.manifest_hold_hours}-hour hold.
            </li>
            <li>
              Expired leftovers: swept weekly once {policy.orphans.min_age_days} days old. Staged source items are kept{" "}
              {policy.existingStage31.stagedItemRetentionDays} days (Stage 3.1).
            </li>
            <li>Never deleted: {policy.permanent.join("; ")}.</li>
          </ul>
        </section>
      )}

      {notice && (
        <p role="status" className="mt-6 rounded-lg border border-line bg-white px-4 py-3 text-sm text-ink">
          {notice}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-6 text-sm font-medium text-purple">
          {error}
        </p>
      )}
      {jobs === null && !error && (
        <p role="status" className="mt-10 flex items-center gap-3 text-ink-muted">
          <Loader2 className="size-5 animate-spin text-purple" aria-hidden /> Loading cleanup jobs…
        </p>
      )}
      {jobs?.length === 0 && (
        <p className="mt-10 rounded-[var(--radius-md)] border border-dashed border-purple/25 bg-white/60 px-6 py-10 text-center text-ink-muted">
          No cleanup jobs yet. A job is planned automatically once an edition is published.
        </p>
      )}
      {jobs && jobs.length > 0 && (
        <ul className="mt-10 divide-y divide-line overflow-hidden rounded-[var(--radius-md)] border border-line bg-white">
          {jobs.map((j) => (
            <li key={j.id}>
              <button
                type="button"
                onClick={() => void toggle(j.id)}
                aria-expanded={open === j.id}
                className="on-light flex w-full flex-wrap items-center gap-x-6 gap-y-2 px-5 py-4 text-left transition hover:bg-purple/[0.03]"
              >
                <span className="eyebrow w-24 text-ink-muted">{j.editionSlug ?? "orphans"}</span>
                <span className="min-w-0 flex-1">
                  <span className="block font-semibold text-ink">
                    {j.kind === "published_week" && j.windowStart && j.windowEnd
                      ? `Week ${weekLabel(j.windowStart, j.windowEnd)}`
                      : "Expired leftovers sweep"}
                  </span>
                  <span className="block text-sm text-ink-muted">
                    {j.deleted} deleted · {j.protected} protected · {j.skipped} skipped
                    {j.failures ? ` · ${j.failures} failed` : ""}
                    {j.status === "scheduled" ? ` · earliest ${when(j.notBefore)}` : ""}
                    {j.status === "staged" ? ` · deletes from ${when(j.executeAfter)}` : ""}
                    {j.status === "failed" && j.nextRetryAt ? ` · retry ${when(j.nextRetryAt)}` : ""}
                  </span>
                </span>
                <Badge status={j.status} />
                <ChevronDown className={`size-4 text-purple transition ${open === j.id ? "rotate-180" : ""}`} aria-hidden />
              </button>
              {open === j.id && (
                <div className="border-t border-line bg-soft/60 px-5 py-5 text-sm text-ink">
                  {j.blockedReasons.length > 0 && (
                    <p className="mb-3 text-purple">Blocked: {j.blockedReasons.map(reasonLabel).join("; ")}. Nothing is deleted until this is fixed.</p>
                  )}
                  {j.lastError && <p className="mb-3 text-purple">Last error: {j.lastError}</p>}
                  {!detail && <Loader2 className="size-4 animate-spin text-purple" aria-label="Loading" />}
                  {detail && (
                    <>
                      <table className="w-full max-w-2xl text-left">
                        <thead className="text-ink-muted">
                          <tr>
                            <th className="py-1 font-medium">Record</th>
                            <th className="py-1 font-medium">State</th>
                            <th className="py-1 font-medium">Why</th>
                            <th className="py-1 text-right font-medium">Count</th>
                          </tr>
                        </thead>
                        <tbody>
                          {detail.manifest.map((m) => (
                            <tr key={`${m.target}-${m.state}-${m.reason}`} className="border-t border-line">
                              <td className="py-1">{m.target.replaceAll("_", " ")}</td>
                              <td className="py-1">{m.state}</td>
                              <td className="py-1">{reasonLabel(m.reason)}</td>
                              <td className="py-1 text-right tabular-nums">{m.count}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <p className="mt-3 text-ink-muted">
                        Planned {when(detail.createdAt)} · policy {detail.policyVersion} · {detail.retries} retr
                        {detail.retries === 1 ? "y" : "ies"}
                        {detail.completedAt ? ` · completed ${when(detail.completedAt)}` : ""}
                      </p>
                      <details className="mt-3">
                        <summary className="cursor-pointer font-semibold text-purple">Audit trail</summary>
                        <ul className="mt-2 space-y-1 text-ink-muted">
                          {detail.audit.map((a, i) => (
                            <li key={i}>
                              {when(a.occurredAt)} — {a.action.replaceAll("_", " ")} ({a.actorKind})
                            </li>
                          ))}
                        </ul>
                      </details>
                    </>
                  )}
                  <div className="mt-4 flex flex-wrap gap-3">
                    {j.status === "failed" && (
                      <button type="button" className="btn-secondary" disabled={busy !== null}
                        onClick={() => void act("retry", () => api.cleanup.retry(j.id), "Retry scheduled for the next run.")}>
                        <RotateCcw className="size-4" aria-hidden /> Retry
                      </button>
                    )}
                    {canCancel(j.status) && (
                      <button type="button" className="btn-secondary" disabled={busy !== null}
                        onClick={() => void act("cancel", () => api.cleanup.cancel(j.id), "Cancelled. Nothing was deleted.")}>
                        <XCircle className="size-4" aria-hidden /> Cancel
                      </button>
                    )}
                    {j.status === "cancelled" && (
                      <button type="button" className="btn-secondary" disabled={busy !== null}
                        onClick={() => void act("reopen", () => api.cleanup.reopen(j.id), "Reopened with a fresh safety hold.")}>
                        <RotateCcw className="size-4" aria-hidden /> Reopen
                      </button>
                    )}
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

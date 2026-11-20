"use client";

import { ArrowRight, Loader2, RefreshCw, Sparkles } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { weekLabel } from "@/lib/admin/editor";
import type { EditionSummary } from "@/lib/admin/types";
import { useAdmin } from "./AdminShell";
import { StatusBadge } from "./StatusBadge";

export function EditionList() {
  const { api } = useAdmin();
  const [items, setItems] = useState<EditionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setItems((await api.list()).items);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load editions.");
    }
  }, [api]);

  useEffect(() => {
    void load();
  }, [load]);

  async function generate() {
    setBusy(true);
    setNotice(null);
    try {
      const report = await api.generate("previous");
      setNotice(report.message);
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Generation failed.");
    } finally {
      setBusy(false);
    }
  }

  const waiting = items?.filter((e) => e.status === "review_required" || e.status === "approved").length ?? 0;

  return (
    <div>
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div>
          <h1 className="display text-[clamp(2.2rem,5vw,3.6rem)] text-ink">Weekly editions</h1>
          <p className="mt-3 max-w-2xl text-ink-muted">
            Editions are generated automatically every Monday from the previous week’s ranked stories. Nothing reaches
            students until an editor approves and publishes it.
            {waiting > 0 && <strong className="text-ink"> {waiting} waiting for you.</strong>}
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          <button type="button" className="btn-secondary" onClick={() => void load()}>
            <RefreshCw className="size-4" aria-hidden /> Refresh
          </button>
          <button type="button" className="btn-primary" onClick={() => void generate()} disabled={busy}>
            {busy ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Sparkles className="size-4" aria-hidden />}
            Generate last week’s edition
          </button>
        </div>
      </div>
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

      {items === null && !error && (
        <p role="status" className="mt-10 flex items-center gap-3 text-ink-muted">
          <Loader2 className="size-5 animate-spin text-purple" aria-hidden /> Loading editions…
        </p>
      )}
      {items?.length === 0 && (
        <p className="mt-10 rounded-[var(--radius-md)] border border-dashed border-purple/25 bg-white/60 px-6 py-10 text-center text-ink-muted">
          No editions yet. The first one is generated on the Monday after a complete week.
        </p>
      )}
      {items && items.length > 0 && (
        <ul className="mt-10 divide-y divide-line overflow-hidden rounded-[var(--radius-md)] border border-line bg-white">
          {items.map((e) => (
            <li key={e.id}>
              <Link href={`/admin/editions/${e.id}`} className="on-light flex flex-wrap items-center gap-x-6 gap-y-2 px-5 py-4 transition hover:bg-purple/[0.03]">
                <span className="eyebrow w-24 text-ink-muted">{e.slug}</span>
                <span className="min-w-0 flex-1">
                  <span className="block font-semibold text-ink">{e.title || "Untitled edition"}</span>
                  <span className="block text-sm text-ink-muted">
                    Week {weekLabel(e.weekStart, e.weekEnd)} · {e.storyCount} stories · revision {e.currentRevision}
                    {e.status === "generation_failed" && e.lastError ? ` · ${e.lastError}` : ""}
                  </span>
                </span>
                <StatusBadge status={e.status} />
                <ArrowRight className="size-4 text-purple" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

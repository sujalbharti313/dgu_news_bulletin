"use client";

import { ArrowUpRight, Search, Sparkles } from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/States";
import { API_BASE_URL } from "@/lib/api/config";
import { formatDate, topicLabels } from "@/lib/format";
import { searchStories } from "@/lib/search";
import type { LoadState, Page, Story } from "@/lib/types";

const suggestions = ["open models", "agents", "Indian languages", "retrieval", "model card", "research papers"];

/** Keyword search through the API when it is configured; otherwise local keyword search over loaded stories. */
function useArchiveResults(query: string, stories: Story[]): LoadState<Story[]> {
  const [remote, setRemote] = useState<LoadState<Story[]>>({ status: "ready", data: [] });
  const local = useMemo(() => searchStories(stories, { query }), [stories, query]);

  useEffect(() => {
    if (!API_BASE_URL || !query.trim()) return;
    const controller = new AbortController();
    setRemote({ status: "loading" });
    fetch(`${API_BASE_URL}/api/v1/search?q=${encodeURIComponent(query)}&pageSize=50`, { signal: controller.signal })
      .then((r) => {
        if (!r.ok) throw new Error(`Search responded with ${r.status}`);
        return r.json() as Promise<Page<Story>>;
      })
      .then((data) => setRemote({ status: "ready", data: data.items }))
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setRemote({ status: "error", message: err instanceof Error ? err.message : "Search failed" });
      });
    return () => controller.abort();
  }, [query]);

  if (!API_BASE_URL) return { status: "ready", data: local };
  return remote;
}

export function ArchiveSearch({ stories }: { stories: Story[] }) {
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query.trim());
  const results = useArchiveResults(deferred, stories);

  return (
    <section id="ask" aria-labelledby="ask-title" className="relative overflow-hidden bg-soft py-24 sm:py-32">
      <div className="shell">
        <div className="mx-auto max-w-4xl text-center">
          <p className="eyebrow inline-flex items-center gap-3 text-purple">
            <span className="text-ink-muted">08</span>
            <span className="h-px w-8 bg-purple/30" aria-hidden />
            Ask the AI archive
          </p>
          <h2 id="ask-title" className="display mt-5 text-[clamp(2.5rem,6vw,5rem)] text-ink">
            Ask anything we’ve covered.
          </h2>

          <form role="search" onSubmit={(e) => e.preventDefault()} className="relative mx-auto mt-10 max-w-3xl">
            <label htmlFor="archive-query" className="sr-only">
              Search the archive
            </label>
            <Search className="pointer-events-none absolute left-6 top-1/2 size-6 -translate-y-1/2 text-purple" aria-hidden />
            <input
              id="archive-query"
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Try “run a model on my laptop”"
              className="h-16 w-full rounded-full sm:h-18 border border-purple/15 bg-white pl-16 pr-6 text-lg text-ink shadow-[0_30px_60px_-30px_rgb(82_43_96/0.45)] outline-none transition placeholder:text-ink-muted/60 focus:border-purple focus:ring-4 focus:ring-purple/10"
              autoComplete="off"
            />
          </form>

          <div className="mt-5 flex flex-wrap justify-center gap-2" aria-label="Suggested queries">
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setQuery(s)}
                className="rounded-full border border-purple/20 bg-white px-4 py-1.5 text-sm text-ink transition hover:border-purple hover:text-purple on-light"
              >
                {s}
              </button>
            ))}
          </div>
          <p className="mt-6 inline-flex items-center gap-2 text-sm text-ink-muted">
            <Sparkles className="size-4 text-purple" aria-hidden />
            {API_BASE_URL
              ? "Keyword search through the AI//CAMPUS API. Semantic, AI-powered search is planned."
              : "Keyword search over the sample archive. Semantic, AI-powered search arrives with the backend."}
          </p>
        </div>

        <div className="mx-auto mt-12 max-w-4xl" aria-live="polite">
          {!deferred ? null : results.status === "loading" ? (
            <LoadingState label="Searching the archive" />
          ) : results.status === "error" ? (
            <ErrorState title="Search is unavailable right now" message={results.message} />
          ) : results.data.length === 0 ? (
            <EmptyState title={`Nothing matches “${deferred}”`}>Try a broader term, or one of the suggestions above.</EmptyState>
          ) : (
            <>
              <p className="eyebrow mb-3 text-ink-muted">
                {results.data.length} result{results.data.length === 1 ? "" : "s"}
              </p>
              <ul className="divide-y divide-line overflow-hidden rounded-[var(--radius-md)] bg-white">
                {results.data.slice(0, 6).map((story) => (
                  <li key={story.slug}>
                    <Link href={`/stories/${story.slug}`} className="group flex items-start gap-5 px-5 py-5 transition hover:bg-soft/60 on-light sm:px-7">
                      <div className="min-w-0 flex-1">
                        <p className="eyebrow text-purple">
                          {topicLabels[story.topic]} · {formatDate(story.publishedAt)}
                          {story.provenance === "sample" && " · Sample"}
                        </p>
                        <p className="mt-2 text-lg font-semibold leading-snug text-ink">{story.headline}</p>
                        <p className="mt-1.5 line-clamp-2 text-ink-muted">{story.dek}</p>
                      </div>
                      <ArrowUpRight className="mt-1 size-5 shrink-0 text-purple transition group-hover:-translate-y-0.5 group-hover:translate-x-0.5" aria-hidden />
                    </Link>
                  </li>
                ))}
              </ul>
              {results.data.length > 6 && (
                <Link href={`/archive?q=${encodeURIComponent(deferred)}`} className="mt-4 inline-block font-medium text-purple">
                  See all {results.data.length} results in the archive
                </Link>
              )}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

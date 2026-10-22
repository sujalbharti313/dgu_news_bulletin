"use client";

import { RotateCcw, Search } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { StoryRow } from "@/components/story/StoryRow";
import { EmptyState } from "@/components/ui/States";
import { formatDate, pad2, topicLabels } from "@/lib/format";
import { searchStories } from "@/lib/search";
import type { Edition, Story, Topic } from "@/lib/types";

const PAGE_SIZE = 5;

interface Props {
  editions: Edition[];
  stories: Story[];
}

export function WeeklyArchive({ editions, stories }: Props) {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const [query, setQuery] = useState(params.get("q") ?? "");
  const [topic, setTopic] = useState<Topic | "all">((params.get("topic") as Topic) ?? "all");
  const [editionId, setEditionId] = useState(params.get("edition") ?? "all");
  const [from, setFrom] = useState(params.get("from") ?? "");
  const [to, setTo] = useState(params.get("to") ?? "");
  const [visible, setVisible] = useState(PAGE_SIZE);
  const deferredQuery = useDeferredValue(query);

  const topics = useMemo(() => Array.from(new Set(stories.map((s) => s.topic))), [stories]);
  const results = useMemo(
    () => searchStories(stories, { query: deferredQuery, topic, editionId, from: from || undefined, to: to || undefined }),
    [stories, deferredQuery, topic, editionId, from, to],
  );

  // Keep the URL shareable without adding history entries on every keystroke.
  useEffect(() => {
    const next = new URLSearchParams();
    if (deferredQuery.trim()) next.set("q", deferredQuery.trim());
    if (topic !== "all") next.set("topic", topic);
    if (editionId !== "all") next.set("edition", editionId);
    if (from) next.set("from", from);
    if (to) next.set("to", to);
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    setVisible(PAGE_SIZE);
  }, [deferredQuery, topic, editionId, from, to, pathname, router]);

  const filtersActive = query || topic !== "all" || editionId !== "all" || from || to;
  const reset = () => {
    setQuery("");
    setTopic("all");
    setEditionId("all");
    setFrom("");
    setTo("");
  };

  const editionById = new Map(editions.map((e) => [e.id, e]));
  const fieldClass =
    "h-12 w-full rounded-xl border border-line bg-white px-4 text-ink outline-none transition focus:border-purple focus:ring-4 focus:ring-purple/10";

  return (
    <div className="grid gap-12 lg:grid-cols-12">
      <aside aria-label="Archive filters" className="lg:col-span-4">
        <div className="space-y-7 lg:sticky lg:top-28">
          <form role="search" onSubmit={(e) => e.preventDefault()}>
            <label htmlFor="archive-search" className="eyebrow text-ink">
              Search
            </label>
            <div className="relative mt-3">
              <Search className="pointer-events-none absolute left-4 top-1/2 size-5 -translate-y-1/2 text-purple" aria-hidden />
              <input
                id="archive-search"
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Keyword, topic or idea"
                className={`${fieldClass} pl-12`}
              />
            </div>
          </form>

          <fieldset>
            <legend className="eyebrow text-ink">Topic</legend>
            <div className="mt-3 flex flex-wrap gap-2">
              {(["all", ...topics] as (Topic | "all")[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  aria-pressed={topic === t}
                  onClick={() => setTopic(t)}
                  className={`rounded-full border px-3.5 py-1.5 text-sm transition on-light ${
                    topic === t ? "border-purple bg-purple text-white" : "border-line bg-white text-ink hover:border-purple/50"
                  }`}
                >
                  {t === "all" ? "All topics" : topicLabels[t]}
                </button>
              ))}
            </div>
          </fieldset>

          <div>
            <label htmlFor="archive-edition" className="eyebrow text-ink">
              Edition
            </label>
            <select id="archive-edition" value={editionId} onChange={(e) => setEditionId(e.target.value)} className={`${fieldClass} mt-3`}>
              <option value="all">All editions</option>
              {editions.map((e) => (
                <option key={e.id} value={e.id}>
                  {pad2(e.number)} · {e.title}
                  {e.provenance === "sample" ? " (sample)" : ""}
                </option>
              ))}
            </select>
          </div>

          <fieldset>
            <legend className="eyebrow text-ink">Published between</legend>
            <div className="mt-3 grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="archive-from" className="text-sm text-ink-muted">
                  From
                </label>
                <input id="archive-from" type="date" value={from} max={to || undefined} onChange={(e) => setFrom(e.target.value)} className={`${fieldClass} mt-1 px-3`} />
              </div>
              <div>
                <label htmlFor="archive-to" className="text-sm text-ink-muted">
                  To
                </label>
                <input id="archive-to" type="date" value={to} min={from || undefined} onChange={(e) => setTo(e.target.value)} className={`${fieldClass} mt-1 px-3`} />
              </div>
            </div>
          </fieldset>

          {filtersActive && (
            <button type="button" onClick={reset} className="inline-flex items-center gap-2 text-sm font-medium text-purple on-light">
              <RotateCcw className="size-4" aria-hidden /> Clear all filters
            </button>
          )}
        </div>
      </aside>

      <section aria-labelledby="archive-results" className="lg:col-span-8">
        <div className="flex items-end justify-between border-b border-ink pb-3">
          <h2 id="archive-results" className="eyebrow text-ink" aria-live="polite">
            {results.length} {results.length === 1 ? "story" : "stories"}
          </h2>
          <span className="eyebrow text-ink-muted">Keyword search</span>
        </div>

        {results.length === 0 ? (
          <div className="mt-8">
            <EmptyState title="No stories match these filters">
              <button type="button" onClick={reset} className="font-medium text-purple underline underline-offset-4">
                Clear filters
              </button>{" "}
              to see every edition.
            </EmptyState>
          </div>
        ) : (
          <>
            {results.slice(0, visible).map((story, i) => {
              const edition = editionById.get(story.editionId);
              const prev = results[i - 1];
              const showEdition = edition && (!prev || prev.editionId !== story.editionId);
              return (
                <div key={story.slug}>
                  {showEdition && (
                    <p className="eyebrow mt-8 flex items-center gap-3 text-purple">
                      Edition {pad2(edition.number)} · Week of {formatDate(edition.weekOf)}
                      <span className="h-px flex-1 bg-purple/15" aria-hidden />
                    </p>
                  )}
                  <StoryRow story={story} index={i + 1} />
                </div>
              );
            })}
            {visible < results.length && (
              <div className="mt-10 flex justify-center">
                <button
                  type="button"
                  onClick={() => setVisible((v) => v + PAGE_SIZE)}
                  className="inline-flex h-12 items-center rounded-full border border-purple px-7 font-medium text-purple transition hover:bg-purple hover:text-white on-light"
                >
                  Load more ({results.length - visible} remaining)
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

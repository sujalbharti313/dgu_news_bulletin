"use client";

import { ArrowRight, CornerDownLeft, Search, X } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useDeferredValue, useId, useMemo, useState } from "react";
import { useModalDialog } from "@/components/ui/useModalDialog";
import { formatDate, topicLabels } from "@/lib/format";
import { searchStories } from "@/lib/search";
import type { Story } from "@/lib/types";

interface Props {
  open: boolean;
  onClose: () => void;
  stories: Story[];
}

export function SearchDialog({ open, onClose, stories }: Props) {
  const ref = useModalDialog(open, onClose);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const deferred = useDeferredValue(query);
  const router = useRouter();
  const listId = useId();

  const results = useMemo(
    () => (deferred.trim() ? searchStories(stories, { query: deferred }).slice(0, 6) : stories.slice(0, 4)),
    [deferred, stories],
  );

  const go = (href: string) => {
    ref.current?.close();
    router.push(href);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const hit = results[active];
      go(hit ? `/stories/${hit.slug}` : `/archive?q=${encodeURIComponent(query)}`);
    }
  };

  return (
    <dialog
      ref={ref}
      aria-label="Search stories"
      className="mx-auto mt-[10vh] w-[min(42rem,calc(100%-2rem))] max-w-none rounded-[var(--radius-lg)] bg-white p-0 text-ink shadow-2xl backdrop:bg-night/70 backdrop:backdrop-blur-sm open:animate-[dialog-in_0.35s_var(--ease-out)] on-light"
    >
      <div className="flex items-center gap-3 border-b border-line px-5">
        <Search className="size-5 shrink-0 text-purple" aria-hidden />
        <label htmlFor="global-search" className="sr-only">
          Search stories
        </label>
        <input
          id="global-search"
          autoFocus
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="Search agents, open models, India…"
          className="h-16 w-full bg-transparent text-lg outline-none focus-visible:outline-none placeholder:text-ink-muted/70"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-activedescendant={results[active] ? `${listId}-${results[active].slug}` : undefined}
          autoComplete="off"
        />
        <button type="button" onClick={() => ref.current?.close()} className="rounded-full p-2 hover:bg-soft" aria-label="Close search">
          <X className="size-5" aria-hidden />
        </button>
      </div>

      <div className="px-3 py-3">
        <p className="eyebrow px-3 pb-2 pt-1 text-ink-muted">
          {query.trim() ? `${results.length} result${results.length === 1 ? "" : "s"}` : "Recent stories"} · keyword search
        </p>
        {results.length === 0 ? (
          <p className="px-3 py-8 text-center text-ink-muted">No stories match “{query}”. Try a broader term.</p>
        ) : (
          <ul id={listId} role="listbox" aria-label="Search results" className="space-y-1">
            {results.map((story, i) => (
              <li key={story.slug} id={`${listId}-${story.slug}`} role="option" aria-selected={i === active}>
                <Link
                  href={`/stories/${story.slug}`}
                  onClick={() => ref.current?.close()}
                  onMouseEnter={() => setActive(i)}
                  className={`flex items-start gap-4 rounded-2xl px-3 py-3 transition-colors ${i === active ? "bg-soft" : ""}`}
                >
                  <span className="mt-1 size-2 shrink-0 rounded-full bg-purple" aria-hidden />
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium leading-snug">{story.headline}</span>
                    <span className="mt-1 block text-sm text-ink-muted">
                      {topicLabels[story.topic]} · {formatDate(story.publishedAt)}
                      {story.provenance === "sample" && " · Sample"}
                    </span>
                  </span>
                  {i === active && <CornerDownLeft className="mt-1 size-4 text-ink-muted" aria-hidden />}
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="flex items-center justify-between border-t border-line px-6 py-3 text-sm">
        <span className="text-ink-muted">↑↓ to move · Enter to open</span>
        <button
          type="button"
          onClick={() => go(`/archive${query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ""}`)}
          className="inline-flex items-center gap-1.5 font-medium text-purple"
        >
          Open full archive <ArrowRight className="size-4" aria-hidden />
        </button>
      </div>
    </dialog>
  );
}

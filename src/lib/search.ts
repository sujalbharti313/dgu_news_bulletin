import type { Story, Topic } from "./types";

/**
 * Local keyword search over the loaded stories. This is deliberately simple
 * and is NOT semantic or AI-powered; the archive UI labels it as such. A
 * future /api/v1/search endpoint backed by Qdrant can replace it.
 */

const tokenize = (text: string) =>
  text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9\s-]/g, " ")
    .split(/\s+/)
    .filter((t) => t.length > 1)
    // Naive plural folding so "agents" also finds "agent"
    .map((t) => (t.length > 3 && t.endsWith("s") && !t.endsWith("ss") ? t.slice(0, -1) : t));

export interface StoryFilters {
  query?: string;
  topic?: Topic | "all";
  from?: string;
  to?: string;
  editionId?: string | "all";
}

function score(story: Story, terms: string[]): number {
  if (terms.length === 0) return 1;
  const headline = story.headline.toLowerCase();
  const dek = story.dek.toLowerCase();
  const rest = [story.body.join(" "), story.impact.explore.join(" "), story.impact.whyItMatters, story.topic]
    .join(" ")
    .toLowerCase();
  let total = 0;
  for (const term of terms) {
    const hit = (headline.includes(term) ? 5 : 0) + (dek.includes(term) ? 3 : 0) + (rest.includes(term) ? 1 : 0);
    if (hit === 0) return 0; // every term must match somewhere
    total += hit;
  }
  return total;
}

export function searchStories(stories: Story[], filters: StoryFilters): Story[] {
  const terms = tokenize(filters.query ?? "");
  return stories
    .filter((s) => !filters.topic || filters.topic === "all" || s.topic === filters.topic)
    .filter((s) => !filters.editionId || filters.editionId === "all" || s.editionId === filters.editionId)
    .filter((s) => !filters.from || s.publishedAt >= filters.from)
    .filter((s) => !filters.to || s.publishedAt <= filters.to)
    .map((s) => ({ s, rank: score(s, terms) }))
    .filter(({ rank }) => rank > 0)
    .sort((a, b) => b.rank - a.rank || b.s.publishedAt.localeCompare(a.s.publishedAt))
    .map(({ s }) => s);
}

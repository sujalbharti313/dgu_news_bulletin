import { indiaEcosystem, researchPapers, tools } from "@/data/reference";
import { careerSignals, sampleBriefing, sampleCampusItems } from "@/data/sample/campus";
import { sampleEditions, sampleStories } from "@/data/sample/stories";
import type {
  Briefing,
  CampusItem,
  CareerSignal,
  Edition,
  IndiaEntry,
  Provenance,
  ResearchPaper,
  Story,
  Tool,
} from "@/lib/types";
import { isLiveApiConfigured } from "./config";
import { apiGet, apiGetAll, NotFoundError } from "./http";

/**
 * Single entry point for content. When NEXT_PUBLIC_API_BASE_URL is set, data
 * comes from the FastAPI service; otherwise the clearly labelled sample
 * adapter is used. Live failures are reported as errors and never silently
 * replaced with sample content.
 */

export type DataResult<T> =
  | { ok: true; data: T; source: Provenance }
  | { ok: false; error: string };

async function load<T>(live: () => Promise<T>, sample: () => T): Promise<DataResult<T>> {
  if (!isLiveApiConfigured) return { ok: true, data: sample(), source: "sample" };
  try {
    return { ok: true, data: await live(), source: "live" };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "Unknown error" };
  }
}

const byDateDesc = (a: Story, b: Story) => b.publishedAt.localeCompare(a.publishedAt);

export function getEditions() {
  return load<Edition[]>(
    () => apiGetAll<Edition>("/api/v1/editions"),
    () => [...sampleEditions].sort((a, b) => b.weekOf.localeCompare(a.weekOf)),
  );
}

export function getStories(editionId?: string) {
  const query = editionId ? `?edition=${encodeURIComponent(editionId)}` : "";
  return load<Story[]>(
    () => apiGetAll<Story>(`/api/v1/stories${query}`),
    () => sampleStories.filter((s) => !editionId || s.editionId === editionId).sort(byDateDesc),
  );
}

/** Stories for the most recent edition, featured story first. */
export async function getCurrentEdition(): Promise<DataResult<{ edition: Edition; stories: Story[] }>> {
  if (isLiveApiConfigured) {
    try {
      const { stories: rawStories, ...edition } = await apiGet<Edition & { stories: Story[] }>("/api/v1/editions/current");
      const stories = [...rawStories].sort((a, b) => Number(!!b.featured) - Number(!!a.featured));
      return { ok: true, source: "live", data: { edition, stories } };
    } catch (err) {
      if (err instanceof NotFoundError) return { ok: false, error: "No editions have been published yet." };
      return { ok: false, error: err instanceof Error ? err.message : "Unknown error" };
    }
  }
  const editions = await getEditions();
  if (!editions.ok) return editions;
  const edition = editions.data[0];
  if (!edition) return { ok: false, error: "No editions have been published yet." };
  const stories = await getStories(edition.id);
  if (!stories.ok) return stories;
  const ordered = [...stories.data].sort((a, b) => Number(!!b.featured) - Number(!!a.featured));
  return { ok: true, source: stories.source, data: { edition, stories: ordered } };
}

/** Resolves to null when the story does not exist. */
export async function getStory(slug: string): Promise<DataResult<Story | null>> {
  if (!isLiveApiConfigured) {
    return { ok: true, source: "sample", data: sampleStories.find((s) => s.slug === slug) ?? null };
  }
  try {
    const story = await apiGet<Story>(`/api/v1/stories/${encodeURIComponent(slug)}`);
    return { ok: true, source: "live", data: story };
  } catch (err) {
    if (err instanceof NotFoundError) return { ok: true, source: "live", data: null };
    return { ok: false, error: err instanceof Error ? err.message : "Unknown error" };
  }
}

export function getLatestBriefing() {
  return load<Briefing>(() => apiGet<Briefing>("/api/v1/briefings/latest"), () => sampleBriefing);
}

/* Reference sections. Without the API these come from the static, source-linked files in src/data. */

export function getTools() {
  return load<Tool[]>(() => apiGetAll<Tool>("/api/v1/tools"), () => tools);
}

export function getResearchPapers() {
  return load<ResearchPaper[]>(() => apiGetAll<ResearchPaper>("/api/v1/research?sort=oldest"), () => researchPapers);
}

export function getIndiaEcosystem() {
  return load<IndiaEntry[]>(() => apiGetAll<IndiaEntry>("/api/v1/india"), () => indiaEcosystem);
}

export function getCareerSignals() {
  return load<CareerSignal[]>(() => apiGetAll<CareerSignal>("/api/v1/career-signals"), () => careerSignals);
}

export function getCampusItems() {
  return load<CampusItem[]>(() => apiGetAll<CampusItem>("/api/v1/campus"), () => sampleCampusItems);
}

/**
 * Shared content model for AI//CAMPUS.
 * Shapes mirror the planned FastAPI responses so the mock adapter can be
 * swapped for live endpoints without touching presentation components.
 */

export type Topic =
  | "models"
  | "research"
  | "tools"
  | "policy"
  | "india"
  | "careers"
  | "campus";

/** Where a record came from. Sample records must never be presented as real news. */
export type Provenance = "sample" | "live";

export interface StoryImage {
  src: string;
  alt: string;
  credit: string;
}

export interface StudentImpact {
  whyItMatters: string;
  explore: string[];
  buildIdea: string;
  disciplines: string[];
}

export interface Story {
  slug: string;
  provenance: Provenance;
  editionId: string;
  headline: string;
  dek: string;
  body: string[];
  topic: Topic;
  sourceName: string;
  /** Null until a verified original URL exists. */
  sourceUrl: string | null;
  publishedAt: string; // ISO date
  readingMinutes: number;
  impact: StudentImpact;
  image?: StoryImage;
  featured?: boolean;
}

export interface Edition {
  id: string;
  number: number;
  title: string;
  weekOf: string; // ISO date, Monday of the edition week
  provenance: Provenance;
}

export type ToolCategory =
  | "coding"
  | "agents"
  | "media"
  | "research"
  | "open-models"
  | "infrastructure";

export interface Tool {
  id: string;
  name: string;
  maker: string;
  category: ToolCategory;
  summary: string;
  goodFor: string[];
  limitations: string;
  access: string;
  officialUrl: string;
}

export interface ResearchPaper {
  id: string;
  title: string;
  authors: string;
  organisation: string;
  published: string; // ISO date of first arXiv version
  plainLanguage: string;
  demonstrated: string;
  limitations: string;
  url: string;
}

export interface IndiaEntry {
  id: string;
  name: string;
  kind: "Government programme" | "Research lab" | "Startup" | "Public platform";
  focus: string;
  description: string;
  url: string;
}

export interface CareerSignal {
  id: string;
  kind: "skill" | "project" | "open-source";
  title: string;
  description: string;
  startWith: string;
  relatedTopics: Topic[];
}

export interface CampusItem {
  id: string;
  provenance: Provenance;
  kind: "Project" | "Workshop" | "Showcase" | "Hackathon" | "Club";
  title: string;
  description: string;
  when?: string;
}

export interface Briefing {
  editionId: string;
  title: string;
  /** Null until a real recorded or synthesised audio file exists. */
  audioUrl: string | null;
  durationSeconds: number | null;
  transcript: string[];
  provenance: Provenance;
}

export type LoadState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

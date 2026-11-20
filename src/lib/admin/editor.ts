/**
 * Pure editing logic of the admin review page (no React, no network), so it can be unit tested with
 * `npm test`. The backend re-validates everything; these checks only give the editor early feedback.
 */
import type { Candidate, EditionDetail, EditionStatus, Issue, StoryText } from "./types";

/** Defaults of backend/config/edition.toml [limits]. The backend enforces the configured values. */
export const HINT_LIMITS = {
  title: 120,
  introduction: 1200,
  editorial_note: 600,
  headline: 200,
  summary: 600,
  technical_explanation: 1800,
  why_it_matters: 900,
  student_relevance: 900,
  project_idea: 500,
} as const;

export const STORY_FIELDS: { key: keyof StoryText; label: string; rows: number; required: boolean }[] = [
  { key: "headline", label: "Headline", rows: 2, required: true },
  { key: "summary", label: "Summary", rows: 4, required: true },
  { key: "technical_explanation", label: "Technical explanation", rows: 5, required: false },
  { key: "why_it_matters", label: "Why it matters", rows: 3, required: true },
  { key: "student_relevance", label: "Why students should care", rows: 3, required: true },
  { key: "project_idea", label: "What you could build (project idea)", rows: 3, required: false },
];

export interface DraftStory {
  clusterId: string;
  text: StoryText;
  publisher: string;
  sourceUrl: string;
  category: string | null;
  eligibility: string;
  reviewFlags: string[];
  /** Added by the editor in this session (not yet saved). */
  added: boolean;
}

export interface Draft {
  title: string;
  introduction: string;
  editorialNote: string;
  stories: DraftStory[];
  /** review_required stories the editor explicitly accepted. */
  acknowledged: string[];
}

export function draftFromDetail(detail: EditionDetail): Draft | null {
  const c = detail.content;
  if (!c) return null;
  return {
    title: c.title,
    introduction: c.introduction,
    editorialNote: c.editorial_note ?? "",
    stories: c.stories.map((s) => ({
      clusterId: s.cluster_id,
      text: { ...s.text },
      publisher: s.publisher,
      sourceUrl: s.source_url,
      category: s.category,
      eligibility: s.selection.eligibility,
      reviewFlags: s.selection.review_flags ?? [],
      added: false,
    })),
    acknowledged: [],
  };
}

export function moveStory(draft: Draft, index: number, delta: -1 | 1): Draft {
  const to = index + delta;
  if (index < 0 || index >= draft.stories.length || to < 0 || to >= draft.stories.length) return draft;
  const stories = [...draft.stories];
  [stories[index], stories[to]] = [stories[to], stories[index]];
  return { ...draft, stories };
}

export function removeStory(draft: Draft, clusterId: string): Draft {
  return {
    ...draft,
    stories: draft.stories.filter((s) => s.clusterId !== clusterId),
    acknowledged: draft.acknowledged.filter((id) => id !== clusterId),
  };
}

export function addCandidate(draft: Draft, c: Candidate, acknowledgeReview: boolean): Draft {
  if (draft.stories.some((s) => s.clusterId === c.clusterId)) return draft;
  if (c.eligibility !== "eligible" && !acknowledgeReview) return draft;
  const story: DraftStory = {
    clusterId: c.clusterId,
    text: { ...c.text },
    publisher: c.publisher,
    sourceUrl: c.sourceUrl,
    category: c.category,
    eligibility: c.eligibility,
    reviewFlags: c.reviewFlags,
    added: true,
  };
  const acknowledged = c.eligibility === "eligible" ? draft.acknowledged : [...draft.acknowledged, c.clusterId];
  return { ...draft, stories: [...draft.stories, story], acknowledged };
}

export function updateStoryText(draft: Draft, clusterId: string, key: keyof StoryText, value: string): Draft {
  return {
    ...draft,
    stories: draft.stories.map((s) => (s.clusterId === clusterId ? { ...s, text: { ...s.text, [key]: value } } : s)),
  };
}

/** The PUT /content body. Only editorial text and story order: source links and status come from the server. */
export function toPayload(draft: Draft, expectedRevision: number) {
  return {
    expectedRevision,
    title: draft.title,
    introduction: draft.introduction,
    editorialNote: draft.editorialNote,
    stories: draft.stories.map((s) => ({
      clusterId: s.clusterId,
      headline: s.text.headline,
      summary: s.text.summary,
      technicalExplanation: s.text.technical_explanation,
      whyItMatters: s.text.why_it_matters,
      studentRelevance: s.text.student_relevance,
      projectIdea: s.text.project_idea,
    })),
    acknowledgeReview: draft.acknowledged,
  };
}

export function isDirty(draft: Draft | null, original: Draft | null): boolean {
  if (!draft || !original) return false;
  return JSON.stringify(toPayload(draft, 0)) !== JSON.stringify(toPayload(original, 0));
}

/** Client-side checks mirroring the backend's error rules (missing fields, lengths, links in text). */
export function localIssues(draft: Draft): Issue[] {
  const issues: Issue[] = [];
  const err = (code: string, message: string, path: string) => issues.push({ severity: "error", code, message, path });
  const url = /\bhttps?:\/\/|\bwww\./i;
  if (!draft.title.trim()) err("missing_field", "The edition needs a title.", "title");
  if (draft.title.length > HINT_LIMITS.title) err("too_long", `Title is over ${HINT_LIMITS.title} characters.`, "title");
  if (!draft.introduction.trim()) err("missing_field", "The edition needs an introduction.", "introduction");
  if (draft.introduction.length > HINT_LIMITS.introduction)
    err("too_long", `Introduction is over ${HINT_LIMITS.introduction} characters.`, "introduction");
  if (draft.editorialNote.length > HINT_LIMITS.editorial_note)
    err("too_long", `Editorial note is over ${HINT_LIMITS.editorial_note} characters.`, "editorialNote");
  for (const [field, value] of [
    ["title", draft.title],
    ["introduction", draft.introduction],
    ["editorialNote", draft.editorialNote],
  ] as const) {
    if (url.test(value)) err("url_in_text", "Links are not allowed in edition text; each story links its source.", field);
  }
  if (draft.stories.length === 0) err("no_stories", "An edition needs at least one story.", "stories");
  draft.stories.forEach((s, i) => {
    for (const f of STORY_FIELDS) {
      const value = s.text[f.key];
      const path = `stories.${i}.${f.key}`;
      if (f.required && !value.trim()) err("missing_field", `Story ${i + 1}: ${f.label} is empty.`, path);
      if (value.length > HINT_LIMITS[f.key]) err("too_long", `Story ${i + 1}: ${f.label} is over ${HINT_LIMITS[f.key]} characters.`, path);
      if (url.test(value)) err("url_in_text", `Story ${i + 1}: ${f.label} contains a link.`, path);
    }
  });
  return issues;
}

export const STATUS_LABELS: Record<EditionStatus, string> = {
  generating: "Generating",
  draft: "Draft",
  review_required: "Needs review",
  approved: "Approved",
  published: "Published",
  generation_failed: "Generation failed",
  rejected: "Rejected",
  archived: "Archived",
  withdrawn: "Withdrawn",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status as EditionStatus] ?? status;
}

/** Monday 2 – Sunday 8 November 2026 style label for an edition week. */
export function weekLabel(weekStart: string, weekEnd: string): string {
  const fmt = (iso: string, opts: Intl.DateTimeFormatOptions) =>
    new Intl.DateTimeFormat("en-IN", { timeZone: "UTC", ...opts }).format(new Date(`${iso}T00:00:00Z`));
  const sameMonth = weekStart.slice(0, 7) === weekEnd.slice(0, 7);
  const start = fmt(weekStart, sameMonth ? { day: "numeric" } : { day: "numeric", month: "short" });
  return `${start} – ${fmt(weekEnd, { day: "numeric", month: "short", year: "numeric" })}`;
}

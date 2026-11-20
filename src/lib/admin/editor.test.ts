// Run with `npm test` (Node's built-in test runner with type stripping; no extra dependencies).
import assert from "node:assert/strict";
import { test } from "node:test";
import { errorFromResponse } from "./errors.ts";
import { needsRefresh, parseTokenResponse } from "./session.ts";
import {
  addCandidate,
  draftFromDetail,
  isDirty,
  localIssues,
  moveStory,
  removeStory,
  toPayload,
  updateStoryText,
  weekLabel,
} from "./editor.ts";
import type { Candidate, EditionDetail, StoryText } from "./types.ts";

const text = (n: number): StoryText => ({
  headline: `Headline ${n}`,
  summary: `Summary of story ${n}.`,
  technical_explanation: "",
  why_it_matters: "It matters.",
  student_relevance: "Students can try it.",
  project_idea: "",
});

function detail(): EditionDetail {
  const story = (n: number) => ({
    cluster_id: `c${n}`,
    text: text(n),
    original: text(n),
    source_url: `https://example.org/${n}`,
    publisher: "Example",
    source_kind: null,
    published_date: null,
    category: null,
    topic: "models",
    disciplines: [],
    explore: [],
    verification_note: "",
    source_count: 1,
    selection: {
      source: "ranking" as const,
      eligibility: "eligible",
      rank: n,
      shortlist_rank: n,
      final_priority_score: 50,
      priority: "Medium",
      review_flags: [],
      ranking_run_id: null,
      review_acknowledged_by: null,
    },
  });
  return {
    content: {
      title: "Week title",
      introduction: "An introduction.",
      editorial_note: "",
      week_start: "2026-11-16",
      week_end: "2026-11-22",
      timezone: "Asia/Kolkata",
      stories: [story(1), story(2), story(3)],
      composition: null,
    },
  } as unknown as EditionDetail;
}

const candidate = (id: string, eligibility: string): Candidate => ({
  clusterId: id,
  eligibility,
  rank: 9,
  finalPriorityScore: 40,
  priority: "Low",
  reviewFlags: eligibility === "eligible" ? [] : ["single_source"],
  selectionNote: null,
  category: null,
  publisher: "Example",
  sourceUrl: "https://example.org/x",
  publishedDate: null,
  text: text(9),
});

test("reorder, remove and edit change the draft without mutating the original", () => {
  const original = draftFromDetail(detail())!;
  const moved = moveStory(original, 0, 1);
  assert.deepEqual(moved.stories.map((s) => s.clusterId), ["c2", "c1", "c3"]);
  assert.deepEqual(original.stories.map((s) => s.clusterId), ["c1", "c2", "c3"]);
  assert.equal(moveStory(original, 0, -1), original); // out of range is a no-op
  const removed = removeStory(moved, "c1");
  assert.deepEqual(removed.stories.map((s) => s.clusterId), ["c2", "c3"]);
  const edited = updateStoryText(original, "c3", "headline", "New headline");
  assert.equal(edited.stories[2].text.headline, "New headline");
  assert.equal(original.stories[2].text.headline, "Headline 3");
  assert.ok(isDirty(edited, original) && !isDirty(original, draftFromDetail(detail())));
});

test("adding a review_required candidate needs an explicit acknowledgement", () => {
  const d = draftFromDetail(detail())!;
  assert.equal(addCandidate(d, candidate("c9", "review_required"), false), d);
  const ok = addCandidate(d, candidate("c9", "review_required"), true);
  assert.equal(ok.stories.length, 4);
  assert.deepEqual(ok.acknowledged, ["c9"]);
  assert.equal(addCandidate(ok, candidate("c9", "review_required"), true), ok); // no duplicates
  assert.deepEqual(removeStory(ok, "c9").acknowledged, []);
  assert.deepEqual(addCandidate(d, candidate("c8", "eligible"), false).acknowledged, []);
});

test("payload carries only editorial text and order, never status or source links", () => {
  const payload = toPayload(draftFromDetail(detail())!, 4);
  assert.equal(payload.expectedRevision, 4);
  assert.deepEqual(Object.keys(payload.stories[0]).sort(), [
    "clusterId",
    "headline",
    "projectIdea",
    "studentRelevance",
    "summary",
    "technicalExplanation",
    "whyItMatters",
  ]);
  assert.ok(!("status" in payload));
});

test("local checks flag missing fields, overlong text and links", () => {
  const d = draftFromDetail(detail())!;
  assert.deepEqual(localIssues(d), []);
  const bad = updateStoryText(updateStoryText(d, "c1", "summary", " "), "c2", "why_it_matters", "See https://x.example");
  const codes = localIssues({ ...bad, title: "x".repeat(200) }).map((i) => i.code);
  assert.ok(codes.includes("missing_field") && codes.includes("url_in_text") && codes.includes("too_long"));
  assert.ok(localIssues({ ...d, stories: [] }).some((i) => i.code === "no_stories"));
});

test("week labels", () => {
  assert.equal(weekLabel("2026-11-16", "2026-11-22"), "16 – 22 Nov 2026");
  assert.equal(weekLabel("2026-11-30", "2026-12-06"), "30 Nov – 6 Dec 2026");
});

test("token responses and refresh timing", () => {
  const now = 1_000_000;
  const s = parseTokenResponse({ access_token: "a", refresh_token: "r", expires_in: 3600, user: { email: "e@x.in" } }, now)!;
  assert.equal(s.email, "e@x.in");
  assert.equal(s.expiresAt, now + 3_600_000);
  assert.equal(parseTokenResponse({ access_token: "a" }, now), null);
  assert.equal(parseTokenResponse(null, now), null);
  assert.ok(!needsRefresh(s, now) && needsRefresh(s, now + 3_590_000));
});

test("API errors use the server message but never echo auth details", () => {
  const e = errorFromResponse(409, { error: { code: "stale_revision", message: "Someone else saved a newer revision." } });
  assert.equal(e.code, "stale_revision");
  assert.match(e.message, /newer revision/);
  assert.match(errorFromResponse(401, { error: { code: "unauthorized", message: "jwt expired" } }).message, /Sign in again/);
  assert.match(errorFromResponse(500, null).message, /500/);
});

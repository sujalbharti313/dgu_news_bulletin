import assert from "node:assert/strict";
import { test } from "node:test";
import { canCancel, cleanupStatusLabel, reasonLabel, relativeTime } from "./cleanup.ts";

test("cleanup labels are plain language and fall back safely", () => {
  assert.equal(cleanupStatusLabel("blocked"), "Blocked (publication not confirmed)");
  assert.equal(cleanupStatusLabel("something_new"), "something_new");
  assert.equal(reasonLabel("in_edition_selection"), "Selected in an edition");
  assert.equal(reasonLabel("new_reason_code"), "new reason code");
  assert.equal(reasonLabel(null), "");
});

test("only jobs that have not run can be cancelled", () => {
  for (const s of ["scheduled", "staged", "blocked", "failed"]) assert.ok(canCancel(s));
  for (const s of ["running", "completed", "cancelled"]) assert.ok(!canCancel(s));
});

test("relative times", () => {
  const now = Date.parse("2026-11-25T10:00:00Z");
  assert.equal(relativeTime("2026-11-28T10:00:00Z", now), "in 3 days");
  assert.equal(relativeTime("2026-11-25T08:00:00Z", now), "2 hours ago");
  assert.equal(relativeTime(null, now), "—");
});

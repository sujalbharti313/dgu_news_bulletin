/** Plain-language labels for Stage 3.6 cleanup jobs (pure; unit tested with `npm test`). */

const STATUS: Record<string, string> = {
  scheduled: "Waiting for safety delay",
  blocked: "Blocked (publication not confirmed)",
  staged: "Manifest saved, waiting",
  running: "Deleting",
  completed: "Completed",
  failed: "Failed (will retry)",
  cancelled: "Cancelled",
};

const REASONS: Record<string, string> = {
  in_edition_selection: "Selected in an edition",
  in_edition_revision: "Part of an edition revision",
  active_edition_candidate: "Candidate of an unpublished edition",
  ranked_in_other_week: "Belongs to another week",
  referenced_by_edition: "Ranking an edition was built from",
  latest_completed_run: "Latest completed ranking of the week",
  database_protected: "Refused by the database guard",
  still_in_pipeline: "Still in the news pipeline",
  recently_active: "Recently updated by the pipeline",
  locked: "In use by another process",
  outside_scope: "No longer in this week",
  has_analysis: "Analysis exists again",
  already_absent: "Already gone",
  // Publication checks (blocked jobs)
  not_published: "Edition is not published",
  snapshot_hash_mismatch: "Published snapshot does not match its approval",
  snapshot_missing: "Published snapshot missing",
  published_story_count_mismatch: "Public stories do not match the snapshot",
  source_link_missing: "A source link is missing",
  source_link_mismatch: "A source link differs from the snapshot",
  not_publicly_retrievable: "Edition is not publicly visible",
  publication_failure_after_publish: "A publish failure was recorded after publishing",
  week_not_ended: "The week has not ended",
};

export function cleanupStatusLabel(status: string): string {
  return STATUS[status] ?? status;
}

export function reasonLabel(code: string | null | undefined): string {
  if (!code) return "";
  return REASONS[code] ?? code.replaceAll("_", " ");
}

export function canCancel(status: string): boolean {
  return ["scheduled", "staged", "blocked", "failed"].includes(status);
}

/** "in 3 days" / "2 hours ago" relative to `now` (ms). */
export function relativeTime(iso: string | null, now: number): string {
  if (!iso) return "—";
  const diff = new Date(iso).getTime() - now;
  const abs = Math.abs(diff);
  const units: [number, string][] = [
    [86_400_000, "day"],
    [3_600_000, "hour"],
    [60_000, "minute"],
  ];
  for (const [ms, unit] of units) {
    if (abs >= ms) {
      const n = Math.round(abs / ms);
      const label = `${n} ${unit}${n === 1 ? "" : "s"}`;
      return diff >= 0 ? `in ${label}` : `${label} ago`;
    }
  }
  return "now";
}

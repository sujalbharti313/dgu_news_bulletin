/** Shapes of the Stage 3.5 admin API (backend/app/api/v1/endpoints/admin_editions.py). Admin pages only. */

export type EditionStatus =
  | "generating"
  | "draft"
  | "review_required"
  | "approved"
  | "published"
  | "generation_failed"
  | "rejected"
  | "archived"
  | "withdrawn";

export type AdminAction =
  | "edit"
  | "submit"
  | "approve"
  | "reject"
  | "return_for_revision"
  | "publish"
  | "regenerate"
  | "archive";

export interface StoryText {
  headline: string;
  summary: string;
  technical_explanation: string;
  why_it_matters: string;
  student_relevance: string;
  project_idea: string;
}

export interface StorySelection {
  source: "ranking" | "admin";
  eligibility: string;
  rank: number | null;
  shortlist_rank: number | null;
  final_priority_score: number | null;
  priority: string | null;
  review_flags: string[];
  ranking_run_id: string | null;
  review_acknowledged_by: string | null;
}

export interface EditionStory {
  cluster_id: string;
  text: StoryText;
  original: StoryText;
  source_url: string;
  publisher: string;
  source_kind: string | null;
  published_date: string | null;
  category: string | null;
  topic: string;
  disciplines: string[];
  explore: string[];
  verification_note: string;
  source_count: number;
  selection: StorySelection;
}

export interface EditionContent {
  title: string;
  introduction: string;
  editorial_note: string;
  week_start: string;
  week_end: string;
  timezone: string;
  stories: EditionStory[];
  composition: { mode: string; model: string | null; composedAt: string; rankingRunId: string | null } | null;
}

export interface Issue {
  severity: "error" | "warning";
  code: string;
  message: string;
  path: string | null;
}

export interface AdminPerson {
  id: string;
  email: string;
  name: string | null;
}

export interface EditionSummary {
  id: string;
  slug: string;
  number: number | null;
  title: string;
  weekStart: string;
  weekEnd: string;
  timezone: string;
  status: EditionStatus;
  currentRevision: number;
  storyCount: number;
  generatedAt: string | null;
  approvedAt: string | null;
  publishedAt: string | null;
  generationAttempts: number;
  nextAttemptAt: string | null;
  lastError: string | null;
}

export interface RevisionSummary {
  revision: number;
  origin: string;
  contentHash: string;
  createdAt: string;
  createdBy: AdminPerson | null;
  changes: Record<string, unknown> | null;
  issueCount: number;
}

export interface AuditEntry {
  action: string;
  revision: number | null;
  actorKind: string;
  actor: AdminPerson | null;
  at: string;
  details: Record<string, unknown> | null;
}

export interface EditionDetail extends EditionSummary {
  content: EditionContent | null;
  contentHash: string | null;
  issues: Issue[];
  allowedActions: AdminAction[];
  generation: Record<string, unknown> | null;
  approval: { revision: number | null; contentHash: string | null; by: AdminPerson | null; at: string | null };
  publication: { revision: number | null; by: AdminPerson | null; at: string | null };
  reviewNote: string | null;
  revisions: RevisionSummary[];
  audit: AuditEntry[];
}

export interface Candidate {
  clusterId: string;
  eligibility: string;
  rank: number | null;
  finalPriorityScore: number | null;
  priority: string | null;
  reviewFlags: string[];
  selectionNote: string | null;
  category: string | null;
  publisher: string;
  sourceUrl: string;
  publishedDate: string | null;
  text: StoryText;
}

export interface GenerationReport {
  weekStart: string;
  status: string;
  message: string;
  editionId: string | null;
  editionStatus: string | null;
  revision: number | null;
}

/* ---------------------------------------------------------------------------------------------------------------
 * Stage 3.6 cleanup admin API (backend/app/api/v1/endpoints/admin_cleanup.py). Ids, counts and reason codes only.
 * ------------------------------------------------------------------------------------------------------------- */

export type CleanupJobStatus = "scheduled" | "blocked" | "staged" | "running" | "completed" | "failed" | "cancelled";

export interface CleanupJob {
  id: string;
  jobKey: string;
  kind: "published_week" | "expired_orphans";
  editionId: string | null;
  editionSlug?: string | null;
  windowStart: string | null;
  windowEnd: string | null;
  timezone: string;
  status: CleanupJobStatus;
  trigger: string;
  policyVersion: string;
  notBefore: string | null;
  stagedAt: string | null;
  executeAfter: string | null;
  startedAt: string | null;
  lastRunAt: string | null;
  completedAt: string | null;
  nextRetryAt: string | null;
  eligible: number;
  protected: number;
  deleted: number;
  skipped: number;
  failures: number;
  retries: number;
  blockedReasons: string[];
  protectionSummary: Record<string, number>;
  lastError: string | null;
  createdAt: string | null;
}

export interface CleanupManifestRow {
  target: string;
  state: string;
  reason: string | null;
  count: number;
}

export interface CleanupJobDetail extends CleanupJob {
  details: Record<string, unknown>;
  manifest: CleanupManifestRow[];
  items: { target: string; recordId: string; state: string; reason: string | null; attempts: number; processedAt: string | null }[];
  audit: { action: string; actorKind: string; actorId: string | null; policyVersion: string | null; details: Record<string, unknown>; occurredAt: string }[];
}

export interface CleanupPolicy {
  version: string;
  enabled: boolean;
  publishedWeek: { safety_delay_hours: number; min_story_age_days: number; manifest_hold_hours: number; delete_superseded_ranking_runs: boolean };
  orphans: { enabled: boolean; min_age_days: number; manifest_hold_hours: number };
  execution: Record<string, number>;
  activeWindowHours: number;
  firstEditionWeek: string | null;
  existingStage31: { stagedItemRetentionDays: number; jobRecordRetentionDays: number };
  permanent: string[];
}

export interface CleanupRunReport {
  status: string;
  message: string;
  planned: number;
  staged: number;
  deleted: number;
  deferred: string | null;
  failures: string[];
}

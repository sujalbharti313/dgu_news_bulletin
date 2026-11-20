"use client";

import { ArrowDown, ArrowLeft, ArrowUp, ExternalLink, Eye, Loader2, Plus, Save, Trash2, Undo2 } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useId, useMemo, useState } from "react";
import { AdminApiError } from "@/lib/admin/api";
import {
  addCandidate,
  type Draft,
  draftFromDetail,
  HINT_LIMITS,
  isDirty,
  localIssues,
  moveStory,
  removeStory,
  STORY_FIELDS,
  toPayload,
  updateStoryText,
  weekLabel,
} from "@/lib/admin/editor";
import type { AdminAction, Candidate, EditionDetail, Issue } from "@/lib/admin/types";
import type { Story } from "@/lib/types";
import { useAdmin } from "./AdminShell";
import { StatusBadge } from "./StatusBadge";

type Flash = { tone: "ok" | "error"; text: string; stale?: boolean } | null;

const ACTION_HELP: Partial<Record<AdminAction, string>> = {
  submit: "Send this draft for review.",
  approve: "Approve exactly this revision. Any later edit removes the approval.",
  publish: "Make the approved revision public on the bulletin.",
  regenerate: "Build a new revision from the Stage 3.4 ranking (uses one Gemini request).",
  archive: "Hide this edition from the review queue. It is never shown publicly.",
};

export function EditionReview({ id }: { id: string }) {
  const { api } = useAdmin();
  const [detail, setDetail] = useState<EditionDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [original, setOriginal] = useState<Draft | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const [tab, setTab] = useState<"edit" | "preview" | "history">("edit");

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const d = await api.detail(id);
      setDetail(d);
      const fresh = draftFromDetail(d);
      setDraft(fresh);
      setOriginal(fresh);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Could not load this edition.");
    }
  }, [api, id]);

  useEffect(() => {
    void load();
  }, [load]);

  const dirty = isDirty(draft, original);

  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const run = useCallback(
    async (label: string, fn: () => Promise<{ message?: string } | unknown>, success: string) => {
      setBusy(label);
      setFlash(null);
      try {
        const result = (await fn()) as { message?: string; alreadyApproved?: boolean; alreadyPublished?: boolean } | undefined;
        const note = result?.alreadyApproved ? " (it was already approved)" : result?.alreadyPublished ? " (it was already published)" : "";
        setFlash({ tone: "ok", text: (result?.message ?? success) + note });
        await load();
      } catch (err) {
        const stale = err instanceof AdminApiError && err.code === "stale_revision";
        setFlash({ tone: "error", text: err instanceof Error ? err.message : "That did not work.", stale });
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  if (loadError) {
    return (
      <div>
        <BackLink />
        <p role="alert" className="mt-6 text-purple">
          {loadError}
        </p>
      </div>
    );
  }
  if (!detail) {
    return (
      <p role="status" className="flex items-center gap-3 text-ink-muted">
        <Loader2 className="size-5 animate-spin text-purple" aria-hidden /> Loading edition…
      </p>
    );
  }

  const can = (a: AdminAction) => detail.allowedActions.includes(a);
  const rev = detail.currentRevision;
  const editable = can("edit");
  const pendingIssues = draft && dirty ? localIssues(draft) : [];

  return (
    <div>
      <BackLink />
      <header className="mt-6 flex flex-wrap items-start justify-between gap-6">
        <div className="min-w-0">
          <p className="eyebrow text-ink-muted">
            {detail.slug} · Week {weekLabel(detail.weekStart, detail.weekEnd)} ({detail.timezone})
          </p>
          <h1 className="display mt-3 text-[clamp(2rem,4.5vw,3.2rem)] text-ink">{detail.title || "Untitled edition"}</h1>
          <p className="mt-3 flex flex-wrap items-center gap-3 text-sm text-ink-muted">
            <StatusBadge status={detail.status} />
            <span>Revision {rev}</span>
            {detail.approval.at && (
              <span>
                Approved r{detail.approval.revision} by {who(detail.approval.by)} · {when(detail.approval.at)}
              </span>
            )}
            {detail.publication.at && (
              <span>
                Published r{detail.publication.revision} by {who(detail.publication.by)} · {when(detail.publication.at)}
              </span>
            )}
          </p>
          {detail.reviewNote && (
            <p className="mt-3 max-w-2xl rounded-lg bg-purple/[0.06] px-3 py-2 text-sm text-ink">Review note: {detail.reviewNote}</p>
          )}
          {detail.status === "generation_failed" && (
            <p className="mt-3 max-w-2xl rounded-lg border border-purple/30 px-3 py-2 text-sm text-ink">
              Generation failed after {detail.generationAttempts} attempt(s): {detail.lastError ?? "unknown error"}.
              {detail.nextAttemptAt && ` The scheduler retries after ${when(detail.nextAttemptAt)}.`}
            </p>
          )}
        </div>
      </header>

      {flash && (
        <div
          role={flash.tone === "error" ? "alert" : "status"}
          className={`mt-6 flex flex-wrap items-center gap-3 rounded-lg px-4 py-3 text-sm ${
            flash.tone === "error" ? "border border-purple/40 bg-white text-purple" : "bg-ink text-white"
          }`}
        >
          {flash.text}
          {flash.stale && (
            <button type="button" className="btn-secondary !py-1" onClick={() => void load()}>
              Reload the latest revision (your unsaved edits are discarded)
            </button>
          )}
        </div>
      )}

      <Issues server={detail.issues} local={pendingIssues} dirty={dirty} />

      <Actions
        detail={detail}
        dirty={dirty}
        busy={busy}
        onSubmit={() => run("submit", () => api.submit(id, rev), "Sent for review.")}
        onApprove={() => run("approve", () => api.approve(id, rev, detail.contentHash ?? ""), `Revision ${rev} approved.`)}
        onPublish={() => run("publish", () => api.publish(id, rev), "Published. The edition is now live.")}
        onRegenerate={() => run("regenerate", () => api.regenerate(id, rev), "Regenerated.")}
        onArchive={() => run("archive", () => api.archive(id, rev), "Archived.")}
        onReject={(reason, back) =>
          run("reject", () => api.reject(id, rev, reason, back), back ? "Returned to draft for changes." : "Rejected.")
        }
      />

      <div role="tablist" aria-label="Edition views" className="mt-10 flex gap-2 border-b border-line">
        {(["edit", "preview", "history"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            type="button"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-semibold ${tab === t ? "border-purple text-purple" : "border-transparent text-ink-muted"}`}
          >
            {t === "edit" ? (editable ? "Edit" : "Content") : t === "preview" ? "Preview" : "Revisions & audit"}
          </button>
        ))}
      </div>

      {tab === "edit" && draft && (
        <EditorPanel
          draft={draft}
          setDraft={setDraft}
          editable={editable}
          dirty={dirty}
          busy={busy}
          onReset={() => setDraft(original)}
          onSave={() => run("save", () => api.save(id, toPayload(draft, rev)), "Saved as a new revision.")}
          loadCandidates={() => api.candidates(id).then((r) => r.items)}
          detail={detail}
        />
      )}
      {tab === "edit" && !draft && <p className="mt-8 text-ink-muted">This edition has no content yet.</p>}
      {tab === "preview" && <PreviewPanel id={id} dirty={dirty} revision={rev} />}
      {tab === "history" && <HistoryPanel detail={detail} loadRevision={(n) => api.revision(id, n)} />}
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/admin" className="on-light inline-flex items-center gap-2 text-sm font-medium text-purple">
      <ArrowLeft className="size-4" aria-hidden /> All editions
    </Link>
  );
}

const who = (p: { name: string | null; email: string } | null) => (p ? p.name || p.email : "the scheduler");
const when = (iso: string) =>
  new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" }).format(new Date(iso)) + " IST";

function Issues({ server, local, dirty }: { server: Issue[]; local: Issue[]; dirty: boolean }) {
  const list = dirty ? local : server;
  if (list.length === 0) return null;
  const errors = list.filter((i) => i.severity === "error");
  return (
    <section aria-label="Validation" className="mt-6 rounded-[var(--radius-md)] border border-line bg-white p-5">
      <p className="text-sm font-semibold text-ink">
        {dirty ? "Checks on your unsaved changes" : "Checks on this revision"}
        {errors.length > 0 ? ` · ${errors.length} must be fixed before approval` : " · warnings only"}
      </p>
      <ul className="mt-3 space-y-1.5 text-sm">
        {list.map((i, n) => (
          <li key={n} className={i.severity === "error" ? "text-purple" : "text-ink-muted"}>
            <span className="font-semibold">{i.severity === "error" ? "Error" : "Warning"}:</span> {i.message}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Actions(props: {
  detail: EditionDetail;
  dirty: boolean;
  busy: string | null;
  onSubmit: () => void;
  onApprove: () => void;
  onPublish: () => void;
  onRegenerate: () => void;
  onArchive: () => void;
  onReject: (reason: string, returnForRevision: boolean) => void;
}) {
  const { detail, dirty, busy } = props;
  const can = (a: AdminAction) => detail.allowedActions.includes(a);
  const [confirming, setConfirming] = useState<AdminAction | null>(null);
  const [rejecting, setRejecting] = useState<null | "reject" | "return">(null);
  const [reason, setReason] = useState("");
  const disabled = dirty || busy !== null;

  const confirmable = (action: AdminAction, label: string, onGo: () => void, style = "btn-primary") =>
    can(action) && (
      <span className="inline-flex items-center gap-2">
        {confirming === action ? (
          <>
            <button
              type="button"
              className={style}
              disabled={disabled}
              onClick={() => {
                setConfirming(null);
                onGo();
              }}
            >
              {busy === action && <Loader2 className="size-4 animate-spin" aria-hidden />} Confirm: {label.toLowerCase()}
            </button>
            <button type="button" className="text-sm text-ink-muted underline" onClick={() => setConfirming(null)}>
              Cancel
            </button>
          </>
        ) : (
          <button type="button" className={style} disabled={disabled} title={ACTION_HELP[action]} onClick={() => setConfirming(action)}>
            {label}
          </button>
        )}
      </span>
    );

  const anything = ["submit", "approve", "reject", "return_for_revision", "publish", "regenerate", "archive"].some((a) =>
    can(a as AdminAction),
  );
  if (!anything) return null;

  return (
    <section aria-label="Workflow actions" className="mt-6 rounded-[var(--radius-md)] border border-line bg-white p-5">
      {dirty && <p className="mb-3 text-sm text-ink-muted">Save or discard your edits before approving, publishing or rejecting.</p>}
      {can("approve") && (
        <p className="mb-3 text-sm text-ink-muted">
          Approving records your account against revision {detail.currentRevision} (content {detail.contentHash?.slice(0, 12)}…).
        </p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        {can("submit") && (
          <button type="button" className="btn-primary" disabled={disabled} onClick={props.onSubmit}>
            Submit for review
          </button>
        )}
        {confirmable("approve", "Approve", props.onApprove)}
        {confirmable("publish", "Publish", props.onPublish)}
        {can("return_for_revision") && (
          <button type="button" className="btn-secondary" disabled={disabled} onClick={() => setRejecting("return")}>
            <Undo2 className="size-4" aria-hidden /> Return for changes
          </button>
        )}
        {can("reject") && (
          <button type="button" className="btn-danger" disabled={disabled} onClick={() => setRejecting("reject")}>
            Reject
          </button>
        )}
        {confirmable("regenerate", "Regenerate", props.onRegenerate, "btn-secondary")}
        {confirmable("archive", "Archive", props.onArchive, "btn-danger")}
      </div>
      {rejecting && (
        <form
          className="mt-5 max-w-2xl"
          onSubmit={(e) => {
            e.preventDefault();
            props.onReject(reason.trim(), rejecting === "return");
            setRejecting(null);
            setReason("");
          }}
        >
          <label className="field-label" htmlFor="reject-reason">
            {rejecting === "return" ? "What needs to change?" : "Why is this edition rejected?"}
          </label>
          <textarea
            id="reject-reason"
            className="field"
            rows={3}
            minLength={3}
            maxLength={1000}
            required
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
          <div className="mt-3 flex gap-3">
            <button type="submit" className={rejecting === "return" ? "btn-secondary" : "btn-danger"} disabled={busy !== null || reason.trim().length < 3}>
              {rejecting === "return" ? "Return to draft" : "Reject edition"}
            </button>
            <button type="button" className="text-sm text-ink-muted underline" onClick={() => setRejecting(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </section>
  );
}

function EditorPanel(props: {
  draft: Draft;
  setDraft: (d: Draft) => void;
  editable: boolean;
  dirty: boolean;
  busy: string | null;
  onSave: () => void;
  onReset: () => void;
  loadCandidates: () => Promise<Candidate[]>;
  detail: EditionDetail;
}) {
  const { draft, setDraft, editable } = props;
  const original = useMemo(() => new Map((props.detail.content?.stories ?? []).map((s) => [s.cluster_id, s])), [props.detail]);

  return (
    <div className="mt-8 space-y-8">
      <fieldset disabled={!editable} className="space-y-5 rounded-[var(--radius-md)] border border-line bg-white p-5 sm:p-6">
        <legend className="eyebrow px-2 text-ink-muted">Edition</legend>
        <TextField label="Title" value={draft.title} max={HINT_LIMITS.title} rows={1} onChange={(v) => setDraft({ ...draft, title: v })} />
        <TextField
          label="Introduction"
          value={draft.introduction}
          max={HINT_LIMITS.introduction}
          rows={5}
          onChange={(v) => setDraft({ ...draft, introduction: v })}
        />
        <TextField
          label="Closing note from the editors (optional)"
          value={draft.editorialNote}
          max={HINT_LIMITS.editorial_note}
          rows={2}
          onChange={(v) => setDraft({ ...draft, editorialNote: v })}
        />
      </fieldset>

      <ol className="space-y-5">
        {draft.stories.map((s, i) => {
          const before = original.get(s.clusterId);
          return (
            <li key={s.clusterId} className="rounded-[var(--radius-md)] border border-line bg-white p-5 sm:p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0">
                  <p className="eyebrow text-ink-muted">
                    Story {i + 1}
                    {s.category ? ` · ${s.category}` : ""}
                    {s.added ? " · added (unsaved)" : ""}
                  </p>
                  <a
                    href={s.sourceUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="on-light mt-1 inline-flex items-center gap-1.5 text-sm font-medium text-purple"
                  >
                    {s.publisher} <ExternalLink className="size-3.5" aria-hidden />
                  </a>
                  {s.eligibility !== "eligible" && (
                    <p className="mt-1 text-xs text-ink-muted">
                      Needs review ({s.reviewFlags.join(", ") || s.eligibility}); accepted by an editor.
                    </p>
                  )}
                  {before?.verification_note && <p className="mt-2 max-w-3xl text-xs text-ink-muted">{before.verification_note}</p>}
                </div>
                {editable && (
                  <div className="flex gap-2">
                    <IconButton label="Move up" disabled={i === 0} onClick={() => setDraft(moveStory(draft, i, -1))}>
                      <ArrowUp className="size-4" />
                    </IconButton>
                    <IconButton label="Move down" disabled={i === draft.stories.length - 1} onClick={() => setDraft(moveStory(draft, i, 1))}>
                      <ArrowDown className="size-4" />
                    </IconButton>
                    <IconButton label="Remove story" onClick={() => setDraft(removeStory(draft, s.clusterId))}>
                      <Trash2 className="size-4" />
                    </IconButton>
                  </div>
                )}
              </div>
              <fieldset disabled={!editable} className="mt-5 grid gap-4 lg:grid-cols-2">
                {STORY_FIELDS.map((f) => (
                  <div key={f.key} className={f.key === "headline" || f.key === "summary" ? "lg:col-span-2" : ""}>
                    <TextField
                      label={f.label + (f.required ? "" : " (optional)")}
                      value={s.text[f.key]}
                      max={HINT_LIMITS[f.key]}
                      rows={f.rows}
                      changed={before ? before.original[f.key] !== s.text[f.key] : false}
                      onChange={(v) => setDraft(updateStoryText(draft, s.clusterId, f.key, v))}
                    />
                  </div>
                ))}
              </fieldset>
            </li>
          );
        })}
      </ol>

      {editable && <AddStory draft={draft} setDraft={setDraft} load={props.loadCandidates} />}

      {editable && (
        <div className="sticky bottom-4 z-10 flex flex-wrap items-center gap-3 rounded-full border border-line bg-white/95 px-4 py-3 shadow-lg backdrop-blur">
          <button type="button" className="btn-primary" disabled={!props.dirty || props.busy !== null} onClick={props.onSave}>
            {props.busy === "save" ? <Loader2 className="size-4 animate-spin" aria-hidden /> : <Save className="size-4" aria-hidden />}
            Save as new revision
          </button>
          <button type="button" className="btn-secondary" disabled={!props.dirty || props.busy !== null} onClick={props.onReset}>
            Discard changes
          </button>
          <span className="text-sm text-ink-muted">{props.dirty ? "Unsaved changes" : "No unsaved changes"}</span>
        </div>
      )}
    </div>
  );
}

function TextField(props: { label: string; value: string; max: number; rows: number; changed?: boolean; onChange: (v: string) => void }) {
  const id = useId();
  const over = props.value.length > props.max;
  return (
    <div>
      <div className="flex items-baseline justify-between gap-3">
        <label htmlFor={id} className="field-label">
          {props.label}
          {props.changed && <span className="ml-2 text-xs font-normal text-purple">edited</span>}
        </label>
        <span className={`text-xs ${over ? "font-semibold text-purple" : "text-ink-muted"}`}>
          {props.value.length}/{props.max}
        </span>
      </div>
      {props.rows === 1 ? (
        <input id={id} className="field" value={props.value} onChange={(e) => props.onChange(e.target.value)} />
      ) : (
        <textarea id={id} className="field" rows={props.rows} value={props.value} onChange={(e) => props.onChange(e.target.value)} />
      )}
    </div>
  );
}

function IconButton({ label, children, ...rest }: { label: string; children: React.ReactNode; disabled?: boolean; onClick: () => void }) {
  return (
    <button type="button" aria-label={label} title={label} className="btn-secondary !p-2" {...rest}>
      {children}
    </button>
  );
}

function AddStory({ draft, setDraft, load }: { draft: Draft; setDraft: (d: Draft) => void; load: () => Promise<Candidate[]> }) {
  const [items, setItems] = useState<Candidate[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ack, setAck] = useState<Record<string, boolean>>({});
  const open = items !== null;

  async function show() {
    setError(null);
    try {
      setItems(await load());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load candidates.");
    }
  }

  const available = (items ?? []).filter((c) => !draft.stories.some((s) => s.clusterId === c.clusterId));

  return (
    <section className="rounded-[var(--radius-md)] border border-dashed border-purple/30 bg-white/60 p-5 sm:p-6">
      {!open ? (
        <button type="button" className="btn-secondary" onClick={() => void show()}>
          <Plus className="size-4" aria-hidden /> Add a story from this week’s ranking
        </button>
      ) : (
        <>
          <p className="font-semibold text-ink">Other stories from this week’s Stage 3.4 ranking</p>
          <p className="mt-1 text-sm text-ink-muted">
            Only ranked stories with source records can be added. Stories marked “needs review” were not auto-selected; add
            them only after checking the original source.
          </p>
          {available.length === 0 && <p className="mt-4 text-sm text-ink-muted">No other candidates for this week.</p>}
          <ul className="mt-4 space-y-3">
            {available.map((c) => {
              const needsAck = c.eligibility !== "eligible";
              return (
                <li key={c.clusterId} className="rounded-lg border border-line bg-white p-4">
                  <p className="font-semibold text-ink">{c.text.headline}</p>
                  <p className="mt-1 text-sm text-ink-muted">
                    {c.publisher} · rank {c.rank ?? "–"} · score {c.finalPriorityScore ?? "–"} ·{" "}
                    {needsAck ? `needs review (${c.reviewFlags.join(", ") || c.eligibility})` : "eligible"}
                    {c.selectionNote ? ` · ${c.selectionNote}` : ""}
                  </p>
                  <div className="mt-3 flex flex-wrap items-center gap-4">
                    <a href={c.sourceUrl} target="_blank" rel="noopener noreferrer" className="on-light inline-flex items-center gap-1 text-sm text-purple">
                      Read the source <ExternalLink className="size-3.5" aria-hidden />
                    </a>
                    {needsAck && (
                      <label className="flex items-center gap-2 text-sm text-ink">
                        <input type="checkbox" checked={!!ack[c.clusterId]} onChange={(e) => setAck({ ...ack, [c.clusterId]: e.target.checked })} />
                        I checked the source and accept this story
                      </label>
                    )}
                    <button
                      type="button"
                      className="btn-secondary !py-1.5"
                      disabled={needsAck && !ack[c.clusterId]}
                      onClick={() => setDraft(addCandidate(draft, c, !!ack[c.clusterId]))}
                    >
                      <Plus className="size-4" aria-hidden /> Add
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}
      {error && <p className="mt-3 text-sm text-purple">{error}</p>}
    </section>
  );
}

interface PreviewData {
  title: string;
  introduction: string | null;
  editorialNote: string | null;
  weekOf: string;
  weekEnd: string;
  stories: Story[];
}

function PreviewPanel({ id, dirty, revision }: { id: string; dirty: boolean; revision: number }) {
  const { api } = useAdmin();
  const [data, setData] = useState<PreviewData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    api
      .preview(id)
      .then((d) => live && setData(d as unknown as PreviewData))
      .catch((err) => live && setError(err instanceof Error ? err.message : "Could not load the preview."));
    return () => {
      live = false;
    };
  }, [api, id, revision]);

  if (error) return <p className="mt-8 text-purple">{error}</p>;
  if (!data) return <p className="mt-8 text-ink-muted">Loading preview…</p>;
  return (
    <div className="mt-8">
      <p className="flex items-center gap-2 text-sm text-ink-muted">
        <Eye className="size-4" aria-hidden /> Preview of saved revision {revision}, as students would see it.
        {dirty && " Your unsaved edits are not included."}
      </p>
      <article className="mt-5 rounded-[var(--radius-lg)] bg-night p-6 text-white sm:p-10">
        <p className="eyebrow text-yellow">Week {weekLabel(data.weekOf, data.weekEnd)}</p>
        <h2 className="display mt-3 text-[clamp(2rem,4vw,3rem)]">{data.title}</h2>
        {data.introduction && <p className="mt-5 max-w-3xl text-lg leading-relaxed text-white/80">{data.introduction}</p>}
      </article>
      <ol className="mt-6 space-y-5">
        {data.stories.map((s, i) => (
          <li key={s.slug} className="rounded-[var(--radius-md)] border border-line bg-white p-6">
            <p className="eyebrow text-ink-muted">
              {String(i + 1).padStart(2, "0")} · {s.topic} · {s.readingMinutes} min
            </p>
            <h3 className="display mt-2 text-2xl text-ink">{s.headline}</h3>
            <p className="mt-3 text-ink-muted">{s.dek}</p>
            {s.body.map((p, n) => (
              <p key={n} className="mt-3 leading-relaxed text-ink">
                {p}
              </p>
            ))}
            {s.impact.whyItMatters && (
              <p className="mt-4 text-sm text-ink">
                <strong>Why should a student care?</strong> {s.impact.whyItMatters}
              </p>
            )}
            {s.impact.buildIdea && (
              <p className="mt-2 text-sm text-ink">
                <strong>What you could build:</strong> {s.impact.buildIdea}
              </p>
            )}
            {s.sourceUrl && (
              <a href={s.sourceUrl} target="_blank" rel="noopener noreferrer" className="on-light mt-4 inline-flex items-center gap-1 text-sm font-medium text-purple">
                Original source: {s.sourceName} <ExternalLink className="size-3.5" aria-hidden />
              </a>
            )}
          </li>
        ))}
      </ol>
      {data.editorialNote && <p className="mt-6 max-w-3xl border-l-2 border-purple pl-5 text-ink-muted">{data.editorialNote}</p>}
    </div>
  );
}

function HistoryPanel({ detail, loadRevision }: { detail: EditionDetail; loadRevision: (n: number) => Promise<Record<string, unknown>> }) {
  const [shown, setShown] = useState<{ n: number; data: Record<string, unknown> } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function open(n: number) {
    setError(null);
    try {
      setShown({ n, data: await loadRevision(n) });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load that revision.");
    }
  }

  const content = shown?.data.content as { title?: string; introduction?: string; stories?: { text: { headline: string } }[] } | undefined;

  return (
    <div className="mt-8 grid gap-8 lg:grid-cols-2">
      <section>
        <h2 className="text-lg font-semibold text-ink">Revisions</h2>
        <ul className="mt-3 divide-y divide-line rounded-[var(--radius-md)] border border-line bg-white">
          {[...detail.revisions].reverse().map((r) => (
            <li key={r.revision} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
              <span>
                <strong className="text-ink">r{r.revision}</strong> · {r.origin.replace(/_/g, " ")} by {who(r.createdBy)} · {when(r.createdAt)}
                {r.issueCount > 0 && ` · ${r.issueCount} issue(s)`}
              </span>
              <button type="button" className="text-purple underline" onClick={() => void open(r.revision)}>
                View
              </button>
            </li>
          ))}
        </ul>
        {error && <p className="mt-3 text-sm text-purple">{error}</p>}
        {shown && (
          <div className="mt-4 rounded-[var(--radius-md)] border border-line bg-white p-4 text-sm">
            <p className="font-semibold text-ink">Revision {shown.n}</p>
            <p className="mt-2 text-ink">{content?.title}</p>
            <p className="mt-2 text-ink-muted">{content?.introduction}</p>
            <ol className="mt-3 list-decimal space-y-1 pl-5 text-ink">
              {(content?.stories ?? []).map((s, i) => (
                <li key={i}>{s.text.headline}</li>
              ))}
            </ol>
          </div>
        )}
      </section>
      <section>
        <h2 className="text-lg font-semibold text-ink">Audit log</h2>
        <ul className="mt-3 divide-y divide-line rounded-[var(--radius-md)] border border-line bg-white">
          {detail.audit.map((a, i) => (
            <li key={i} className="px-4 py-3 text-sm">
              <strong className="text-ink">{a.action.replace(/_/g, " ")}</strong>
              {a.revision !== null && ` · r${a.revision}`} · {a.actor ? who(a.actor) : a.actorKind} · {when(a.at)}
              {a.details && typeof a.details.reason === "string" && <span className="block text-ink-muted">{a.details.reason}</span>}
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}

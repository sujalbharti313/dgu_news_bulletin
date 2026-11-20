import { statusLabel } from "@/lib/admin/editor";

const TONES: Record<string, string> = {
  review_required: "bg-yellow text-ink",
  approved: "bg-purple text-white",
  published: "bg-ink text-white",
  generation_failed: "border border-purple text-purple",
  rejected: "border border-ink/40 text-ink",
  draft: "bg-purple/10 text-purple",
  generating: "bg-purple/10 text-purple",
  archived: "bg-ink/5 text-ink-muted",
  withdrawn: "bg-ink/5 text-ink-muted",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold ${TONES[status] ?? "bg-ink/5 text-ink"}`}>
      {statusLabel(status)}
    </span>
  );
}

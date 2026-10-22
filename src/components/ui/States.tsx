import { AlertTriangle, Inbox, Loader2 } from "lucide-react";

export function LoadingState({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-3 rounded-[var(--radius-md)] border border-line bg-white/60 px-6 py-12 text-ink-muted">
      <Loader2 className="size-5 animate-spin text-purple" aria-hidden />
      {label}…
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-[var(--radius-md)] border border-dashed border-purple/25 bg-white/60 px-6 py-14 text-center">
      <Inbox className="size-7 text-purple" aria-hidden />
      <p className="text-lg font-semibold text-ink">{title}</p>
      {children && <div className="max-w-md text-ink-muted">{children}</div>}
    </div>
  );
}

export function ErrorState({ title = "This content could not load", message }: { title?: string; message?: string }) {
  return (
    <div role="alert" className="flex flex-col items-center gap-3 rounded-[var(--radius-md)] border border-purple/20 bg-white px-6 py-14 text-center">
      <AlertTriangle className="size-7 text-purple" aria-hidden />
      <p className="text-lg font-semibold text-ink">{title}</p>
      {message && <p className="max-w-md text-sm text-ink-muted">{message}</p>}
    </div>
  );
}

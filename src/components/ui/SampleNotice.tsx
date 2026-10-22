import { FlaskConical } from "lucide-react";

/** Persistent disclosure that the visible stories are illustrative, not news. */
export function SampleNotice({ tone = "light", children }: { tone?: "light" | "dark"; children?: React.ReactNode }) {
  return (
    <p
      className={`flex items-start gap-3 rounded-2xl border border-dashed px-4 py-3 text-sm leading-relaxed ${
        tone === "dark" ? "border-white/25 text-white/70" : "border-purple/30 bg-white/70 text-ink-muted"
      }`}
    >
      <FlaskConical className={`mt-0.5 size-4 shrink-0 ${tone === "dark" ? "text-yellow" : "text-purple"}`} aria-hidden />
      <span>
        {children ?? (
          <>
            <strong className={tone === "dark" ? "text-white" : "text-ink"}>Sample edition.</strong> These stories are
            illustrative placeholders that show how AI//CAMPUS will read. They are not news reports and have no original
            article yet. Live, source-linked stories replace them once the news API is connected.
          </>
        )}
      </span>
    </p>
  );
}

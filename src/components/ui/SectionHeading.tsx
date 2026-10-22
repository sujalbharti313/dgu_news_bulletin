interface Props {
  id: string;
  index: string;
  kicker: string;
  title: React.ReactNode;
  intro?: React.ReactNode;
  tone?: "light" | "dark";
  aside?: React.ReactNode;
}

/** Numbered editorial section header used across the homepage. */
export function SectionHeading({ id, index, kicker, title, intro, tone = "light", aside }: Props) {
  const dark = tone === "dark";
  return (
    <header className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-end">
      <div>
        <p className={`eyebrow flex items-center gap-3 ${dark ? "text-lilac" : "text-purple"}`}>
          <span className={dark ? "text-white/40" : "text-ink-muted"}>{index}</span>
          <span className={`h-px w-8 ${dark ? "bg-white/25" : "bg-purple/30"}`} aria-hidden />
          {kicker}
        </p>
        <h2 id={id} className={`display mt-5 max-w-[16ch] text-[clamp(2.5rem,6vw,5rem)] ${dark ? "text-white" : "text-ink"}`}>
          {title}
        </h2>
        {intro && (
          <p className={`mt-6 max-w-[40rem] text-lg leading-relaxed ${dark ? "text-white/70" : "text-ink-muted"}`}>{intro}</p>
        )}
      </div>
      {aside}
    </header>
  );
}

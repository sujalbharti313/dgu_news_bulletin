import { Reveal } from "@/components/motion/Reveal";
import { ExternalLink } from "@/components/ui/ExternalLink";
import type { IndiaEntry } from "@/lib/types";

export function IndiaAISection({ entries }: { entries: IndiaEntry[] }) {
  return (
    <section id="india" aria-labelledby="india-title" className="relative overflow-hidden bg-white py-24 sm:py-32">
      <div className="shell grid gap-14 lg:grid-cols-12">
        <div className="lg:col-span-5">
          <div className="lg:sticky lg:top-28">
            <p className="eyebrow flex items-center gap-3 text-purple">
              <span className="text-ink-muted">05</span>
              <span className="h-px w-8 bg-purple/30" aria-hidden />
              India × AI
            </p>
            <h2 id="india-title" className="display mt-5 text-[clamp(4rem,11vw,9rem)] leading-[0.85] text-purple">
              India
              <span className="block text-ink">
                <span className="text-yellow [-webkit-text-stroke:2px_var(--dbs-ink)]">×</span> AI
              </span>
            </h2>
            <p className="mt-8 max-w-md text-lg leading-relaxed text-ink-muted">
              Who is building AI for India’s languages, infrastructure and public services. Each entry links to the
              organisation’s own site. Dated developments will appear here as the live feed verifies them.
            </p>
          </div>
        </div>

        <ol className="lg:col-span-7">
          {entries.map((entry, i) => (
            <Reveal as="li" key={entry.id} delay={i * 0.05} className="group border-t border-line py-8 last:border-b">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <span className="eyebrow rounded-full bg-deep px-3 py-1 text-white">{entry.kind}</span>
                <span className="text-sm text-ink-muted">{entry.focus}</span>
              </div>
              <h3 className="display mt-5 text-[clamp(2rem,4vw,3rem)] text-ink transition-colors group-hover:text-purple">{entry.name}</h3>
              <p className="mt-3 max-w-[52ch] leading-relaxed text-ink-muted">{entry.description}</p>
              <ExternalLink href={entry.url} className="mt-4 text-sm font-medium text-purple on-light">
                {new URL(entry.url).hostname.replace(/^www\./, "")}
              </ExternalLink>
            </Reveal>
          ))}
        </ol>
      </div>
    </section>
  );
}

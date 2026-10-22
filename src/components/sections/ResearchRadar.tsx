import { Plus } from "lucide-react";
import { Reveal } from "@/components/motion/Reveal";
import { ExternalLink } from "@/components/ui/ExternalLink";
import { SectionHeading } from "@/components/ui/SectionHeading";
import { formatMonth } from "@/lib/format";
import type { ResearchPaper } from "@/lib/types";

/** Uses native <details> so the accordion works without JavaScript. */
export function ResearchRadar({ papers }: { papers: ResearchPaper[] }) {
  return (
    <section id="research" aria-labelledby="research-title" className="bg-soft py-24 sm:py-32">
      <div className="shell">
        <SectionHeading
          id="research-title"
          index="04"
          kicker="Research radar"
          title="Papers, without the hype."
          intro="Plain-language explanations of what a paper actually demonstrated, and what it did not. Until weekly research picks arrive from the live feed, this is our reading list of the foundations behind today’s AI."
        />

        <Reveal className="mt-14">
          <div className="mb-4 flex items-center justify-between gap-4">
            <p className="eyebrow text-ink">Foundations reading list</p>
            <p className="eyebrow text-ink-muted">Dates: first arXiv version</p>
          </div>
          <ol className="border-t-2 border-ink">
            {papers.map((paper, i) => (
              <li key={paper.id} className="border-b border-line">
                <details className="group on-light [&_summary::-webkit-details-marker]:hidden">
                  <summary className="grid cursor-pointer list-none grid-cols-[2.5rem_1fr_auto] items-start gap-4 py-6 transition-colors hover:bg-white/70 sm:grid-cols-[4rem_1fr_10rem_auto] sm:px-3">
                    <span className="display pt-1 text-2xl text-purple/30 group-open:text-purple">{String(i + 1).padStart(2, "0")}</span>
                    <span>
                      <span className="block text-xl font-semibold leading-snug tracking-tight text-ink sm:text-2xl">{paper.title}</span>
                      <span className="mt-1.5 block text-sm text-ink-muted">
                        {paper.authors} · {paper.organisation}
                      </span>
                    </span>
                    <time dateTime={paper.published} className="hidden pt-1.5 text-sm text-ink-muted sm:block">
                      {formatMonth(paper.published)}
                    </time>
                    <span className="mt-1 flex size-9 items-center justify-center rounded-full border border-purple/25 text-purple transition group-open:rotate-45 group-open:bg-purple group-open:text-white">
                      <Plus className="size-4" aria-hidden />
                    </span>
                  </summary>
                  <div className="grid gap-8 pb-8 pl-[3.5rem] pr-2 sm:grid-cols-3 sm:pl-[5.25rem]">
                    <div>
                      <p className="eyebrow text-purple">In plain language</p>
                      <p className="mt-2 leading-relaxed text-ink">{paper.plainLanguage}</p>
                    </div>
                    <div>
                      <p className="eyebrow text-purple">What it demonstrated</p>
                      <p className="mt-2 leading-relaxed text-ink">{paper.demonstrated}</p>
                    </div>
                    <div>
                      <p className="eyebrow text-purple">Limitations</p>
                      <p className="mt-2 leading-relaxed text-ink">{paper.limitations}</p>
                    </div>
                    <div className="sm:col-span-3">
                      <ExternalLink href={paper.url} className="font-medium text-purple">
                        Read the paper on arXiv
                      </ExternalLink>
                    </div>
                  </div>
                </details>
              </li>
            ))}
          </ol>
        </Reveal>
      </div>
    </section>
  );
}

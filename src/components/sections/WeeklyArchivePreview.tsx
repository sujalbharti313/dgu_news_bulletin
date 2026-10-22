import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { formatDate, pad2 } from "@/lib/format";
import type { Edition, Story } from "@/lib/types";

export function WeeklyArchivePreview({ editions, stories }: { editions: Edition[]; stories: Story[] }) {
  return (
    <section id="archive" aria-labelledby="archive-title" className="bg-soft pb-24 pt-4 sm:pb-32">
      <div className="shell">
        <div className="relative overflow-hidden rounded-[var(--radius-lg)] bg-purple px-6 py-12 text-white sm:px-12 sm:py-16">
          <div className="hairline-grid-dark absolute inset-0" aria-hidden />
          <div className="absolute -right-24 -top-24 size-80 rounded-full bg-deep blur-3xl" aria-hidden />
          <div className="relative grid gap-10 lg:grid-cols-12">
            <div className="lg:col-span-5">
              <p className="eyebrow flex items-center gap-3 text-lilac">
                <span className="text-white/50">10</span>
                <span className="h-px w-8 bg-white/30" aria-hidden />
                Weekly archive
              </p>
              <h2 id="archive-title" className="display mt-5 text-[clamp(2.5rem,5vw,4.25rem)]">
                Every edition, searchable.
              </h2>
              <Link
                href="/archive"
                className="mt-8 inline-flex h-12 items-center gap-2 rounded-full bg-yellow px-6 font-semibold text-ink transition hover:-translate-y-0.5"
              >
                Browse the archive <ArrowRight className="size-4" aria-hidden />
              </Link>
            </div>
            <ol className="lg:col-span-7">
              {editions.map((e) => {
                const count = stories.filter((s) => s.editionId === e.id).length;
                return (
                  <li key={e.id} className="border-t border-white/15 last:border-b">
                    <Link href={`/archive?edition=${e.id}`} className="group grid grid-cols-[4rem_1fr_auto] items-center gap-4 py-5">
                      <span className="display text-4xl text-white/30 transition group-hover:text-yellow">{pad2(e.number)}</span>
                      <span>
                        <span className="block text-lg font-semibold">{e.title}</span>
                        <span className="text-sm text-white/60">
                          Week of {formatDate(e.weekOf)} · {count} {count === 1 ? "story" : "stories"}
                          {e.provenance === "sample" && " · Sample"}
                        </span>
                      </span>
                      <ArrowRight className="size-5 text-white/50 transition group-hover:translate-x-1 group-hover:text-yellow" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      </div>
    </section>
  );
}

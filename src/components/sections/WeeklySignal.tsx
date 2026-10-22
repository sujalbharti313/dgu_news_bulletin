import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { Reveal } from "@/components/motion/Reveal";
import { FeaturedStory } from "@/components/story/FeaturedStory";
import { StoryRow } from "@/components/story/StoryRow";
import { StoryTile } from "@/components/story/StoryTile";
import { SampleNotice } from "@/components/ui/SampleNotice";
import { SectionHeading } from "@/components/ui/SectionHeading";
import { EmptyState } from "@/components/ui/States";
import { pad2 } from "@/lib/format";
import type { Edition, Story } from "@/lib/types";

export function WeeklySignal({ edition, stories }: { edition: Edition; stories: Story[] }) {
  const [featured, tile, ...rest] = stories;

  return (
    <div className="bg-night">
      <section id="signal" aria-labelledby="signal-title" className="relative rounded-t-[clamp(28px,5vw,64px)] bg-soft pb-24 pt-20 sm:pb-32 sm:pt-28">
        <div className="shell">
          <SectionHeading
            id="signal-title"
            index="01"
            kicker="Weekly AI intelligence"
            title={
              <>
                The Signal<span className="text-yellow">.</span>
              </>
            }
            intro="What happened in AI this week, why it matters, and where to read the original. Every story ends with something you can explore."
            aside={
              <p className="display text-outline hidden text-[11rem] leading-none text-purple/40 lg:block" aria-label={`Edition ${edition.number}`}>
                {pad2(edition.number)}
              </p>
            }
          />
          {edition.provenance === "sample" && (
            <div className="mt-10 max-w-3xl">
              <SampleNotice />
            </div>
          )}

          {!featured ? (
            <div className="mt-14">
              <EmptyState title="This edition has no stories yet">Check back after the next weekly update.</EmptyState>
            </div>
          ) : (
            <>
              <Reveal className="mt-16">
                <FeaturedStory story={featured} />
              </Reveal>

              {(tile || rest.length > 0) && (
                <div className="mt-20 grid gap-10 lg:grid-cols-12 lg:gap-12">
                  {tile && (
                    <Reveal className="lg:col-span-5">
                      <StoryTile story={tile} />
                    </Reveal>
                  )}
                  <Reveal delay={0.1} className="lg:col-span-7">
                    <div className="flex items-center justify-between border-b border-ink/80 pb-3">
                      <p className="eyebrow text-ink">Also in edition {pad2(edition.number)}</p>
                      <Link href="/archive" className="inline-flex items-center gap-1.5 text-sm font-medium text-purple on-light">
                        All stories <ArrowRight className="size-4" aria-hidden />
                      </Link>
                    </div>
                    {rest.map((story, i) => (
                      <StoryRow key={story.slug} story={story} index={i + 3} />
                    ))}
                  </Reveal>
                </div>
              )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}

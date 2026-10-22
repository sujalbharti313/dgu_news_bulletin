import { ArrowRight } from "lucide-react";
import Link from "next/link";
import type { Story } from "@/lib/types";
import { SourceLink } from "./SourceLink";
import { StoryMeta, TopicPill } from "./StoryMeta";
import { StoryVisual } from "./StoryVisual";

export function FeaturedStory({ story }: { story: Story }) {
  return (
    <article className="group relative grid gap-8 lg:grid-cols-12 lg:gap-12" aria-labelledby={`featured-${story.slug}`}>
      <Link
        href={`/stories/${story.slug}`}
        tabIndex={-1}
        aria-hidden
        className="relative block overflow-hidden rounded-[var(--radius-lg)] bg-night shadow-[0_40px_80px_-40px_rgb(82_43_96/0.6)] lg:col-span-7 lg:min-h-[30rem]"
      >
        <StoryVisual
          slug={story.slug}
          topic={story.topic}
          className="aspect-[4/3] w-full transition duration-[1200ms] ease-[var(--ease-out)] group-hover:scale-[1.035] sm:aspect-[16/10] lg:absolute lg:inset-0 lg:aspect-auto lg:h-full"
        />
        <span className="absolute left-5 top-5 rounded-full bg-yellow px-3 py-1 text-xs font-semibold uppercase tracking-[0.14em] text-ink">
          Featured
        </span>
        <span className="display absolute bottom-4 right-6 text-[6rem] leading-none text-white/10 sm:text-[9rem]" aria-hidden>
          01
        </span>
      </Link>

      <div className="flex flex-col justify-center lg:col-span-5">
        <TopicPill story={story} />
        <h3 id={`featured-${story.slug}`} className="display mt-5 text-[clamp(2rem,3.6vw,3.25rem)] leading-[1] text-ink">
          <Link href={`/stories/${story.slug}`} className="bg-[linear-gradient(var(--dbs-yellow),var(--dbs-yellow))] bg-[length:0%_0.12em] bg-left-bottom bg-no-repeat transition-[background-size] duration-500 hover:bg-[length:100%_0.12em] on-light">
            {story.headline}
          </Link>
        </h3>
        <p className="mt-5 text-lg leading-relaxed text-ink-muted">{story.dek}</p>
        <div className="mt-6">
          <StoryMeta story={story} />
        </div>
        <div className="mt-7 border-l-2 border-yellow bg-white px-5 py-4 shadow-[0_1px_0_var(--line)]">
          <p className="eyebrow text-purple">Why it matters to students</p>
          <p className="mt-2 leading-relaxed text-ink">{story.impact.whyItMatters}</p>
        </div>
        <div className="mt-7 flex flex-wrap items-center gap-x-6 gap-y-3">
          <Link
            href={`/stories/${story.slug}`}
            className="inline-flex h-12 items-center gap-2 rounded-full bg-purple px-6 font-medium text-white transition hover:bg-deep on-light"
          >
            Read the full story <ArrowRight className="size-4" aria-hidden />
          </Link>
          <SourceLink story={story} className="text-purple" />
        </div>
      </div>
    </article>
  );
}

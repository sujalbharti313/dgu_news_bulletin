import { ArrowUpRight } from "lucide-react";
import Link from "next/link";
import { pad2 } from "@/lib/format";
import type { Story } from "@/lib/types";
import { StoryMeta, TopicPill } from "./StoryMeta";

/** Compact, numbered editorial row with a restrained hover lift. */
export function StoryRow({ story, index }: { story: Story; index: number }) {
  return (
    <article className="group relative border-t border-line py-7 transition-colors first:border-t-0 hover:bg-white/70 sm:px-4">
      <div className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-3 sm:grid-cols-[4rem_1fr_auto]">
        <span className="display text-3xl text-purple/25 transition-colors group-hover:text-purple" aria-hidden>
          {pad2(index)}
        </span>
        <div className="min-w-0">
          <TopicPill story={story} />
          <h3 className="mt-3 text-xl font-semibold leading-snug tracking-tight text-ink sm:text-2xl">
            <Link href={`/stories/${story.slug}`} className="after:absolute after:inset-0 on-light">
              {story.headline}
            </Link>
          </h3>
          <p className="mt-2 max-w-[60ch] leading-relaxed text-ink-muted">{story.dek}</p>
          <div className="mt-3">
            <StoryMeta story={story} />
          </div>
        </div>
        <ArrowUpRight
          className="col-start-2 size-6 text-purple transition duration-500 ease-[var(--ease-out)] group-hover:-translate-y-1 group-hover:translate-x-1 sm:col-start-3 sm:row-start-1"
          aria-hidden
        />
      </div>
    </article>
  );
}

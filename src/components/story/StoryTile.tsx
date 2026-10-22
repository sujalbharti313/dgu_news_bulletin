import Link from "next/link";
import type { Story } from "@/lib/types";
import { StoryMeta } from "./StoryMeta";
import { StoryVisual } from "./StoryVisual";
import { topicLabels } from "@/lib/format";

/** A tall, image-led tile used once per edition to break the list rhythm. */
export function StoryTile({ story }: { story: Story }) {
  return (
    <article className="group relative flex h-full flex-col overflow-hidden rounded-[var(--radius-lg)] bg-night text-white">
      <div className="relative overflow-hidden">
        <StoryVisual slug={story.slug} topic={story.topic} className="aspect-[5/4] w-full transition duration-[1200ms] group-hover:scale-[1.04]" />
        <span className="eyebrow absolute left-5 top-5 rounded-full bg-night/60 px-3 py-1 text-white backdrop-blur">
          {topicLabels[story.topic]}
        </span>
      </div>
      <div className="flex flex-1 flex-col p-6 sm:p-8">
        <h3 className="display text-[1.9rem] leading-[1.02]">
          <Link href={`/stories/${story.slug}`} className="after:absolute after:inset-0">
            {story.headline}
          </Link>
        </h3>
        <p className="mt-4 leading-relaxed text-white/70">{story.dek}</p>
        <div className="mt-auto pt-6">
          <StoryMeta story={story} tone="dark" />
        </div>
      </div>
    </article>
  );
}

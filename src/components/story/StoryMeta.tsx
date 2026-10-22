import { formatDate, topicLabels } from "@/lib/format";
import type { Story } from "@/lib/types";

export function TopicPill({ story, tone = "light" }: { story: Story; tone?: "light" | "dark" }) {
  return (
    <span
      className={`eyebrow inline-flex w-fit items-center gap-2 rounded-full px-3 py-1 ${
        tone === "dark" ? "bg-white/10 text-white" : "bg-purple/[0.08] text-purple"
      }`}
    >
      <span className="size-1.5 rounded-full bg-yellow" aria-hidden />
      {topicLabels[story.topic]}
    </span>
  );
}

export function StoryMeta({ story, tone = "light" }: { story: Story; tone?: "light" | "dark" }) {
  const muted = tone === "dark" ? "text-white/60" : "text-ink-muted";
  return (
    <p className={`flex flex-wrap items-center gap-x-2 gap-y-1 text-sm ${muted}`}>
      <span className={tone === "dark" ? "text-white/85" : "text-ink"}>{story.sourceName}</span>
      <span aria-hidden>·</span>
      <time dateTime={story.publishedAt}>{formatDate(story.publishedAt)}</time>
      <span aria-hidden>·</span>
      <span>{story.readingMinutes} min read</span>
      {story.provenance === "sample" && (
        <span className={`sample-tag ml-1 ${tone === "dark" ? "text-yellow/90" : "text-purple/80"}`}>Sample</span>
      )}
    </p>
  );
}

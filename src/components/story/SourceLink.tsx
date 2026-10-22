import { Link2Off } from "lucide-react";
import { ExternalLink } from "@/components/ui/ExternalLink";
import type { Story } from "@/lib/types";

/** Links to the original article, or says plainly that none exists yet. */
export function SourceLink({ story, className = "" }: { story: Story; className?: string }) {
  if (story.sourceUrl) {
    return (
      <ExternalLink href={story.sourceUrl} className={`font-medium ${className}`}>
        Original source: {story.sourceName}
      </ExternalLink>
    );
  }
  return (
    <span className={`inline-flex items-center gap-1.5 text-sm opacity-70 ${className}`}>
      <Link2Off className="size-4" aria-hidden />
      No original article for sample content
    </span>
  );
}

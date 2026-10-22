"use client";

import { Compass, GraduationCap, Hammer, Lightbulb } from "lucide-react";
import Link from "next/link";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useId, useRef, useState } from "react";
import { SectionHeading } from "@/components/ui/SectionHeading";
import { pad2, topicLabels } from "@/lib/format";
import type { Story } from "@/lib/types";

/**
 * Turns each story into four student-facing answers. Implemented as an ARIA
 * tablist with roving focus and arrow-key navigation.
 */
export function StudentImpact({ stories }: { stories: Story[] }) {
  const [active, setActive] = useState(0);
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  const reduce = useReducedMotion();
  const baseId = useId();
  const story = stories[active];
  if (!story) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    const keys: Record<string, number> = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    let next = active;
    if (e.key in keys) next = (active + keys[e.key] + stories.length) % stories.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = stories.length - 1;
    else return;
    e.preventDefault();
    setActive(next);
    tabs.current[next]?.focus();
  };

  const answers = [
    { icon: Lightbulb, label: "Why should a student care?", body: <p>{story.impact.whyItMatters}</p> },
    {
      icon: Compass,
      label: "What to explore",
      body: (
        <ul className="flex flex-wrap gap-2">
          {story.impact.explore.map((item) => (
            <li key={item} className="rounded-full border border-purple/20 bg-purple/[0.05] px-3 py-1 text-sm text-ink">
              {item}
            </li>
          ))}
        </ul>
      ),
    },
    { icon: Hammer, label: "What you could build", body: <p>{story.impact.buildIdea}</p> },
    {
      icon: GraduationCap,
      label: "Relevant disciplines",
      body: <p>{story.impact.disciplines.join(" · ")}</p>,
    },
  ];

  return (
    <section id="impact" aria-labelledby="impact-title" className="relative overflow-hidden bg-white py-24 sm:py-32">
      <div className="hairline-grid pointer-events-none absolute inset-y-0 right-0 w-1/2 opacity-60 [mask-image:linear-gradient(to_left,black,transparent)]" aria-hidden />
      <div className="shell relative">
        <SectionHeading
          id="impact-title"
          index="02"
          kicker="Student impact"
          title="From headline to next step."
          intro="Pick a story. We translate it into what it means for you, what to learn, and what you could build this semester."
        />

        <div className="mt-14 grid gap-8 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:gap-14">
          <div role="tablist" aria-label="Stories" aria-orientation="vertical" className="flex flex-col" onKeyDown={onKeyDown}>
            {stories.map((s, i) => {
              const selected = i === active;
              return (
                <button
                  key={s.slug}
                  ref={(el) => {
                    tabs.current[i] = el;
                  }}
                  id={`${baseId}-tab-${i}`}
                  role="tab"
                  type="button"
                  aria-selected={selected}
                  aria-controls={`${baseId}-panel`}
                  tabIndex={selected ? 0 : -1}
                  onClick={() => setActive(i)}
                  className={`group relative flex items-start gap-4 border-t border-line px-1 py-5 text-left transition-colors on-light last:border-b ${
                    selected ? "text-ink" : "text-ink-muted hover:text-ink"
                  }`}
                >
                  {selected && (
                    <motion.span
                      layoutId={reduce ? undefined : "impact-indicator"}
                      className="absolute -left-3 top-5 bottom-5 w-1 rounded-full bg-yellow"
                      transition={{ type: "spring", stiffness: 380, damping: 34 }}
                      aria-hidden
                    />
                  )}
                  <span className="eyebrow mt-1 w-8 shrink-0">{pad2(i + 1)}</span>
                  <span className="min-w-0">
                    <span className="eyebrow block text-purple/80">{topicLabels[s.topic]}</span>
                    <span className={`mt-1.5 block text-lg leading-snug ${selected ? "font-semibold" : "font-medium"}`}>{s.headline}</span>
                  </span>
                </button>
              );
            })}
          </div>

          <div
            id={`${baseId}-panel`}
            role="tabpanel"
            aria-labelledby={`${baseId}-tab-${active}`}
            tabIndex={0}
            className="relative min-h-[30rem] overflow-hidden rounded-[var(--radius-lg)] bg-soft p-6 on-light sm:p-10"
          >
            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={story.slug}
                initial={reduce ? false : { opacity: 0, y: 18 }}
                animate={{ opacity: 1, y: 0 }}
                exit={reduce ? undefined : { opacity: 0, y: -12 }}
                transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
              >
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p className="eyebrow text-purple">Impact brief · {pad2(active + 1)}</p>
                  {story.provenance === "sample" && <span className="sample-tag text-purple/80">Sample story</span>}
                </div>
                <dl className="mt-8 grid gap-x-10 gap-y-9 sm:grid-cols-2">
                  {answers.map(({ icon: Icon, label, body }) => (
                    <div key={label}>
                      <dt className="flex items-center gap-2.5 font-semibold text-ink">
                        <span className="flex size-8 items-center justify-center rounded-full bg-purple text-white">
                          <Icon className="size-4" aria-hidden />
                        </span>
                        {label}
                      </dt>
                      <dd className="mt-3 leading-relaxed text-ink-muted">{body}</dd>
                    </div>
                  ))}
                </dl>
                <Link href={`/stories/${story.slug}`} className="mt-10 inline-flex items-center gap-2 font-medium text-purple underline decoration-yellow decoration-2 underline-offset-[6px]">
                  Read the story behind this brief
                </Link>
              </motion.div>
            </AnimatePresence>
          </div>
        </div>
      </div>
    </section>
  );
}

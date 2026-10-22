import { BookOpenCheck, GitPullRequest, Hammer } from "lucide-react";
import { Reveal } from "@/components/motion/Reveal";
import { SectionHeading } from "@/components/ui/SectionHeading";
import type { CareerSignal as Signal } from "@/lib/types";

const groups = [
  { kind: "skill", label: "Skills to explore", icon: BookOpenCheck },
  { kind: "project", label: "Project ideas", icon: Hammer },
  { kind: "open-source", label: "Open-source contributions", icon: GitPullRequest },
] as const;

export function CareerSignal({ signals }: { signals: Signal[] }) {
  return (
    <section id="careers" aria-labelledby="careers-title" className="bg-soft py-24 sm:py-32">
      <div className="shell">
        <SectionHeading
          id="careers-title"
          index="06"
          kicker="Career signal"
          title="Skills that compound."
          intro="Educational suggestions tied to this week’s themes. These are not job listings and make no promises about hiring or salaries. Verified internships and competitions will be labelled separately."
        />
        <div className="mt-14 grid gap-5 md:grid-cols-3">
          {groups.map(({ kind, label, icon: Icon }, gi) => (
            <Reveal key={kind} delay={gi * 0.08} className={`rounded-[var(--radius-lg)] p-7 sm:p-8 ${gi === 1 ? "bg-purple text-white md:-translate-y-6" : "bg-white text-ink"}`}>
              <div className="flex items-center gap-3">
                <span className={`flex size-10 items-center justify-center rounded-full ${gi === 1 ? "bg-yellow text-ink" : "bg-purple text-white"}`}>
                  <Icon className="size-5" aria-hidden />
                </span>
                <h3 className="text-lg font-semibold">{label}</h3>
              </div>
              <ul className="mt-6 space-y-6">
                {signals
                  .filter((s) => s.kind === kind)
                  .map((s) => (
                    <li key={s.id} className={`border-t pt-5 ${gi === 1 ? "border-white/15" : "border-line"}`}>
                      <p className="display text-2xl leading-tight">{s.title}</p>
                      <p className={`mt-2 leading-relaxed ${gi === 1 ? "text-white/75" : "text-ink-muted"}`}>{s.description}</p>
                      <p className={`mt-3 text-sm ${gi === 1 ? "text-white" : "text-ink"}`}>
                        <span className={`eyebrow mr-2 ${gi === 1 ? "text-yellow" : "text-purple"}`}>Start</span>
                        {s.startWith}
                      </p>
                    </li>
                  ))}
              </ul>
            </Reveal>
          ))}
        </div>
      </div>
    </section>
  );
}

import { Calendar, FolderGit2, Presentation, Trophy, Users } from "lucide-react";
import Image from "next/image";
import { Reveal } from "@/components/motion/Reveal";
import { ExternalLink } from "@/components/ui/ExternalLink";
import type { CampusItem } from "@/lib/types";

const icons = { Project: FolderGit2, Workshop: Presentation, Showcase: Presentation, Hackathon: Trophy, Club: Users } as const;

export function CampusAI({ items }: { items: CampusItem[] }) {
  return (
    <section id="campus" aria-labelledby="campus-title" className="relative overflow-hidden bg-white py-24 sm:py-32">
      <div className="shell">
        <div className="grid gap-6 lg:grid-cols-12 lg:items-end">
          <div className="lg:col-span-7">
            <p className="eyebrow flex items-center gap-3 text-purple">
              <span className="text-ink-muted">09</span>
              <span className="h-px w-8 bg-purple/30" aria-hidden />
              Campus AI · Dehradun
            </p>
            <h2 id="campus-title" className="display mt-5 text-[clamp(2.5rem,6vw,5rem)] text-ink">
              Where the next signal
              <br />
              <span className="text-purple">gets built.</span>
            </h2>
          </div>
          <p className="text-lg leading-relaxed text-ink-muted lg:col-span-5">
            Student projects, workshops, showcases and hackathons from DBS Global University. Only approved, verified
            campus items will be published here.
          </p>
        </div>

        <Reveal className="mt-14">
          <figure className="relative">
            <div className="relative aspect-[4/3] overflow-hidden rounded-[var(--radius-lg)] sm:aspect-[1220/410]">
              <Image
                src="/campus/dgu-ios-development-center.jpg"
                alt="Rows of iMac workstations in the iOS Development Center at DBS Global University, Dehradun"
                fill
                sizes="(min-width: 1360px) 1264px, 100vw"
                className="object-cover object-[72%_center]"
              />
              <div className="pointer-events-none absolute inset-0 rounded-[var(--radius-lg)] ring-1 ring-inset ring-ink/10" aria-hidden />
            </div>
            <figcaption className="mt-4 flex flex-wrap items-center justify-between gap-2 text-sm text-ink-muted">
              <span>
                <span className="font-medium text-ink">iOS Development Center</span>, DBS Global University, Dehradun
              </span>
              <span>Photo: DBS Global University</span>
            </figcaption>
          </figure>
        </Reveal>

        <div className="mt-16 flex flex-wrap items-center justify-between gap-4 border-b border-ink pb-3">
          <p className="eyebrow text-ink">How campus listings will look</p>
          <span className="sample-tag text-purple/80">Example formats · not real events</span>
        </div>
        <ul className="grid gap-px overflow-hidden rounded-b-[var(--radius-md)] bg-line sm:grid-cols-2 lg:grid-cols-4">
          {items.map((item) => {
            const Icon = icons[item.kind];
            return (
              <li key={item.id} className="flex flex-col bg-white p-6 transition-colors hover:bg-soft">
                <div className="flex items-center justify-between">
                  <span className="flex size-10 items-center justify-center rounded-xl bg-purple/[0.08] text-purple">
                    <Icon className="size-5" aria-hidden />
                  </span>
                  <span className="eyebrow text-ink-muted">{item.kind}</span>
                </div>
                <h3 className="mt-6 text-lg font-semibold text-ink">{item.title}</h3>
                <p className="mt-2 flex-1 text-sm leading-relaxed text-ink-muted">{item.description}</p>
                <p className="mt-5 flex items-center gap-2 text-xs text-ink-muted">
                  <Calendar className="size-3.5" aria-hidden /> {item.when ?? "Date shown once confirmed"}
                </p>
              </li>
            );
          })}
        </ul>
        <div className="mt-8">
          <ExternalLink href="https://dgu.ac.in" className="font-medium text-purple on-light">
            Official university news and events at dgu.ac.in
          </ExternalLink>
        </div>
      </div>
    </section>
  );
}

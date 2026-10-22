import { ArrowDownRight, ArrowRight } from "lucide-react";
import Link from "next/link";
import { formatDate, pad2 } from "@/lib/format";
import type { Edition } from "@/lib/types";
import { HeroVisual } from "./HeroVisual";
import { ScrollIndicator } from "./ScrollIndicator";

export function HeroSection({ edition, storyCount }: { edition: Edition | null; storyCount: number }) {
  return (
    <section aria-labelledby="hero-title" className="relative isolate min-h-[100svh] overflow-hidden bg-night text-white">
      {/* Atmosphere: layered radial light, a faint grid and a horizon line */}
      <div className="absolute inset-0 -z-10" aria-hidden>
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_70%_60%_at_68%_42%,#65176a_0%,rgb(82_43_96/0.55)_38%,transparent_72%)]" />
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_45%_35%_at_12%_100%,rgb(255_222_0/0.10),transparent_70%)]" />
        <div className="hairline-grid-dark absolute inset-0 [mask-image:radial-gradient(ellipse_80%_70%_at_60%_40%,black,transparent)]" />
      </div>

      <HeroVisual />
      {/* Readability scrims keep the network from crossing the headline */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-[70%] bg-gradient-to-t from-night via-night/90 to-transparent md:hidden" aria-hidden />
      <div className="pointer-events-none absolute inset-y-0 left-0 hidden w-[62%] bg-gradient-to-r from-night/85 via-night/45 to-transparent md:block" aria-hidden />

      <div className="shell relative flex min-h-[100svh] flex-col justify-end pb-10 pt-28 sm:pb-14">
        <div className="grid items-end gap-10 lg:grid-cols-[minmax(0,1.35fr)_minmax(0,0.65fr)]">
          <div>
            <p className="eyebrow mb-6 flex items-center gap-3 text-lilac">
              <span className="inline-block size-1.5 rounded-full bg-yellow" aria-hidden />
              DBS Global University / AI Intelligence
            </p>
            <h1 id="hero-title" className="display text-[clamp(3rem,8.6vw,8.25rem)]">
              The future
              <br />
              isn’t waiting.
              <br />
              <span className="text-lilac">
                Neither should you<span className="text-yellow">.</span>
              </span>
            </h1>
            <p className="mt-8 max-w-[34rem] text-lg leading-relaxed text-white/75 sm:text-xl">
              The breakthroughs, tools, and ideas shaping your AI future. Curated for curious minds.
            </p>
            <div className="mt-9 flex flex-col gap-3 xs:flex-row xs:flex-wrap">
              <Link
                href="#signal"
                className="group inline-flex h-14 items-center justify-center gap-3 rounded-full bg-yellow px-7 font-semibold text-ink shadow-[0_10px_40px_-10px_rgb(255_222_0/0.55)] transition hover:-translate-y-0.5"
              >
                Explore this week’s intelligence
                <ArrowDownRight className="size-5 transition group-hover:translate-x-0.5 group-hover:translate-y-0.5" aria-hidden />
              </Link>
              <Link
                href="#tools"
                className="group inline-flex h-14 items-center justify-center gap-3 rounded-full border border-white/25 px-7 font-medium text-white backdrop-blur-sm transition hover:border-white/50 hover:bg-white/5"
              >
                Discover the AI ecosystem
                <ArrowRight className="size-5 transition group-hover:translate-x-0.5" aria-hidden />
              </Link>
            </div>
          </div>

          {edition && (
            <aside
              aria-label="Current edition"
              className="hidden justify-self-end rounded-[var(--radius-md)] border border-white/12 bg-white/[0.04] p-5 backdrop-blur-md lg:block lg:w-full lg:max-w-[19rem]"
            >
              <div className="flex items-center justify-between">
                <p className="eyebrow text-white/50">Edition</p>
                {edition.provenance === "sample" && <span className="sample-tag text-yellow/90">Sample</span>}
              </div>
              <p className="display mt-3 text-6xl text-white">{pad2(edition.number)}</p>
              <p className="mt-3 font-medium text-white/90">{edition.title}</p>
              <dl className="mt-5 grid grid-cols-2 gap-3 border-t border-white/10 pt-4 text-sm">
                <div>
                  <dt className="text-white/45">Week of</dt>
                  <dd className="mt-0.5 text-white/85">{formatDate(edition.weekOf)}</dd>
                </div>
                <div>
                  <dt className="text-white/45">Stories</dt>
                  <dd className="mt-0.5 text-white/85">{storyCount}</dd>
                </div>
              </dl>
            </aside>
          )}
        </div>

        <div className="mt-12 hidden items-center justify-between border-t border-white/10 pt-5 sm:flex">
          <ScrollIndicator href="#signal" />
          <p className="eyebrow hidden text-white/40 sm:block">What changed · Why it matters · What to do next</p>
        </div>
      </div>

      {/* Horizon: dissolves the night scene into the editorial page */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 h-24 bg-gradient-to-b from-transparent to-night" aria-hidden />
    </section>
  );
}

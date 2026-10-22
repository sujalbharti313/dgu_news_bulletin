"use client";

import { Check, CircleAlert, KeyRound } from "lucide-react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { useMemo, useState } from "react";
import { ExternalLink } from "@/components/ui/ExternalLink";
import { SectionHeading } from "@/components/ui/SectionHeading";
import type { Tool, ToolCategory } from "@/lib/types";

type Filter = ToolCategory | "all";

interface Props {
  tools: Tool[];
  categories: { id: ToolCategory; label: string }[];
}

const CX = 200;
const CY = 200;

/** Deterministic polar position for each tool inside its category sector. */
function placeTools(tools: Tool[], categories: Props["categories"]) {
  const sector = (Math.PI * 2) / categories.length;
  return tools.map((tool) => {
    const c = categories.findIndex((cat) => cat.id === tool.category);
    const siblings = tools.filter((t) => t.category === tool.category);
    const k = siblings.indexOf(tool);
    const angle = c * sector - Math.PI / 2 + sector * ((k + 1) / (siblings.length + 1));
    const r = 70 + ((k * 47) % 100);
    return { tool, x: CX + Math.cos(angle) * r, y: CY + Math.sin(angle) * r };
  });
}

export function ToolRadar({ tools, categories }: Props) {
  const [filter, setFilter] = useState<Filter>("all");
  const [selectedId, setSelectedId] = useState(tools[0]?.id);
  const reduce = useReducedMotion();
  const placed = useMemo(() => placeTools(tools, categories), [tools, categories]);
  const visible = filter === "all" ? tools : tools.filter((t) => t.category === filter);
  const selected = tools.find((t) => t.id === selectedId) ?? visible[0];
  const sector = 360 / categories.length;

  const choose = (f: Filter) => {
    setFilter(f);
    const first = f === "all" ? tools[0] : tools.find((t) => t.category === f);
    if (f !== "all" && first && selected?.category !== f) setSelectedId(first.id);
  };

  return (
    <section id="tools" aria-labelledby="tools-title" className="relative overflow-hidden bg-deep py-24 text-white sm:py-32">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_60%_70%_at_20%_40%,#65176a,transparent_70%)]" aria-hidden />
      <div className="hairline-grid-dark absolute inset-0 opacity-70" aria-hidden />
      <div className="shell relative">
        <SectionHeading
          id="tools-title"
          index="03"
          kicker="AI tools & model radar"
          tone="dark"
          title="What’s worth your time to learn."
          intro="Real tools with official links. We describe what each is good for and where it falls short. Prices and limits change often, so check the official page."
        />

        <div className="mt-14 grid gap-12 lg:grid-cols-12">
          {/* Radar */}
          <div className="relative mx-auto w-full max-w-[18rem] sm:max-w-[30rem] lg:col-span-5 lg:mx-0">
            <svg viewBox="0 0 400 400" className="w-full" role="group" aria-label="Tool radar. Use the filters and list for keyboard access.">
              <defs>
                <linearGradient id="sweep" x1="0" y1="0" x2="1" y2="0">
                  <stop offset="0%" stopColor="#ffde00" stopOpacity="0" />
                  <stop offset="100%" stopColor="#ffde00" stopOpacity="0.28" />
                </linearGradient>
              </defs>
              {[60, 110, 160, 190].map((r) => (
                <circle key={r} cx={CX} cy={CY} r={r} fill="none" stroke="#fff" strokeOpacity={r === 190 ? 0.25 : 0.1} />
              ))}
              {categories.map((cat, i) => {
                const a = (i * sector - 90) * (Math.PI / 180);
                const active = filter === cat.id;
                const start = i * sector - 90;
                const end = start + sector;
                const p = (deg: number) => [CX + Math.cos((deg * Math.PI) / 180) * 190, CY + Math.sin((deg * Math.PI) / 180) * 190];
                const [x1, y1] = p(start);
                const [x2, y2] = p(end);
                return (
                  <g key={cat.id}>
                    <path
                      d={`M${CX},${CY} L${x1.toFixed(1)},${y1.toFixed(1)} A190,190 0 0 1 ${x2.toFixed(1)},${y2.toFixed(1)} Z`}
                      fill="#ffde00"
                      fillOpacity={active ? 0.08 : 0}
                      className="transition-[fill-opacity] duration-500"
                    />
                    <line x1={CX} y1={CY} x2={CX + Math.cos(a) * 190} y2={CY + Math.sin(a) * 190} stroke="#fff" strokeOpacity="0.12" />
                  </g>
                );
              })}
              <g style={{ transformOrigin: "200px 200px", animation: reduce ? undefined : "radar-sweep 9s linear infinite" }}>
                <path d={`M${CX},${CY} L${CX + 190},${CY} A190,190 0 0 0 ${(CX + Math.cos(-0.6) * 190).toFixed(1)},${(CY + Math.sin(-0.6) * 190).toFixed(1)} Z`} fill="url(#sweep)" />
              </g>
              {placed.map(({ tool, x, y }) => {
                const dim = filter !== "all" && tool.category !== filter;
                const isSel = selected?.id === tool.id;
                return (
                  <g key={tool.id} className="transition-opacity duration-500" opacity={dim ? 0.18 : 1}>
                    {isSel && <circle cx={x} cy={y} r="13" fill="none" stroke="#ffde00" strokeOpacity="0.8" />}
                    <circle
                      cx={x}
                      cy={y}
                      r={isSel ? 6 : 4.5}
                      fill={isSel ? "#ffde00" : "#efe1f3"}
                      className="cursor-pointer"
                      onClick={() => setSelectedId(tool.id)}
                    >
                      <title>{tool.name}</title>
                    </circle>
                  </g>
                );
              })}
              <circle cx={CX} cy={CY} r="5" fill="#ffde00" />
            </svg>
            <p className="eyebrow mt-4 text-center text-white/45">{visible.length} tools in view</p>
          </div>

          {/* Filters, list and detail */}
          <div className="lg:col-span-7">
            <div role="group" aria-label="Filter tools by category" className="flex flex-wrap gap-2">
              {([{ id: "all", label: "All" }, ...categories] as { id: Filter; label: string }[]).map((cat) => {
                const active = filter === cat.id;
                return (
                  <button
                    key={cat.id}
                    type="button"
                    aria-pressed={active}
                    onClick={() => choose(cat.id)}
                    className={`relative rounded-full border px-4 py-2 text-sm transition-colors ${
                      active ? "border-transparent text-ink" : "border-white/20 text-white/80 hover:border-white/40 hover:text-white"
                    }`}
                  >
                    {active && (
                      <motion.span
                        layoutId={reduce ? undefined : "tool-filter"}
                        className="absolute inset-0 rounded-full bg-yellow"
                        transition={{ type: "spring", stiffness: 420, damping: 34 }}
                        aria-hidden
                      />
                    )}
                    <span className="relative">{cat.label}</span>
                  </button>
                );
              })}
            </div>

            <div className="mt-8 space-y-6">
              <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3" aria-label="Tools">
                {visible.map((tool) => {
                  const isSel = selected?.id === tool.id;
                  return (
                    <li key={tool.id}>
                      <button
                        type="button"
                        aria-pressed={isSel}
                        onClick={() => setSelectedId(tool.id)}
                        className={`flex h-full w-full flex-col rounded-xl border px-4 py-3 text-left transition ${
                          isSel ? "border-white bg-white text-ink" : "border-white/10 text-white/85 hover:border-white/30 hover:bg-white/[0.06]"
                        }`}
                      >
                        <span className="font-semibold leading-tight">{tool.name}</span>
                        <span className={`mt-1 text-xs ${isSel ? "text-ink-muted" : "text-white/50"}`}>{tool.maker}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>

              <div aria-live="polite">
                <AnimatePresence mode="wait" initial={false}>
                  {selected && (
                    <motion.article
                      key={selected.id}
                      initial={reduce ? false : { opacity: 0, x: 16 }}
                      animate={{ opacity: 1, x: 0 }}
                      exit={reduce ? undefined : { opacity: 0, x: -10 }}
                      transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
                      className="grid gap-x-10 gap-y-5 rounded-[var(--radius-md)] border border-white/12 bg-night/50 p-6 backdrop-blur-md sm:grid-cols-2 sm:p-8"
                    >
                      <div>
                      <p className="eyebrow text-lilac">{categories.find((c) => c.id === selected.category)?.label}</p>
                      <h3 className="display mt-3 text-4xl">{selected.name}</h3>
                      <p className="mt-1 text-sm text-white/55">by {selected.maker}</p>
                      <p className="mt-5 leading-relaxed text-white/85">{selected.summary}</p>
                      <ExternalLink href={selected.officialUrl} className="mt-6 font-medium text-yellow">
                        Official site
                      </ExternalLink>
                      </div>
                      <div>
                      <ul className="space-y-2 sm:mt-8">
                        {selected.goodFor.map((g) => (
                          <li key={g} className="flex items-center gap-2.5 text-sm text-white/80">
                            <Check className="size-4 text-yellow" aria-hidden /> {g}
                          </li>
                        ))}
                      </ul>
                      <p className="mt-5 flex gap-2.5 text-sm leading-relaxed text-white/70">
                        <CircleAlert className="mt-0.5 size-4 shrink-0 text-lilac" aria-hidden />
                        {selected.limitations}
                      </p>
                      <p className="mt-3 flex gap-2.5 text-sm leading-relaxed text-white/70">
                        <KeyRound className="mt-0.5 size-4 shrink-0 text-lilac" aria-hidden />
                        {selected.access}
                      </p>
                      </div>
                    </motion.article>
                  )}
                </AnimatePresence>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

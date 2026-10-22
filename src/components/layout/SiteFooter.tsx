import Link from "next/link";
import { UniversityLogo, Wordmark } from "./BrandLockup";
import { navItems } from "./nav-items";

export function SiteFooter() {
  return (
    <footer className="relative overflow-hidden bg-night text-white">
      <div className="hairline-grid-dark absolute inset-0 opacity-60" aria-hidden />
      <div className="shell relative py-16 sm:py-20">
        <div className="grid gap-12 lg:grid-cols-[1.4fr_1fr]">
          <div>
            <p className="display text-[clamp(2.6rem,7vw,5.5rem)] leading-[0.9]">
              Understand what changed.
              <br />
              <span className="text-lilac">Then build what’s next.</span>
            </p>
            <div className="mt-10 flex flex-wrap items-center gap-5">
              <UniversityLogo />
              <p className="max-w-sm text-sm leading-relaxed text-white/60">
                AI//CAMPUS is designed for the DBS Global University community in Dehradun, India. It is a student-focused
                bulletin and is not an official university communication unless a story says so.
              </p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-8 text-sm">
            <div>
              <p className="eyebrow mb-4 text-white/40">Read</p>
              <ul className="space-y-2.5">
                {navItems.map((item) => (
                  <li key={item.href}>
                    <Link href={item.href} className="text-white/75 transition hover:text-white">
                      {item.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <p className="eyebrow mb-4 text-white/40">Editorial</p>
              <ul className="space-y-2.5 text-white/75">
                <li>
                  <Link href="/#impact" className="transition hover:text-white">Student impact</Link>
                </li>
                <li>
                  <Link href="/#briefing" className="transition hover:text-white">60-second briefing</Link>
                </li>
                <li>
                  <Link href="/#ask" className="transition hover:text-white">Ask the archive</Link>
                </li>
                <li>
                  <a href="https://dgu.ac.in" target="_blank" rel="noopener noreferrer" className="transition hover:text-white">
                    dgu.ac.in <span className="sr-only">(opens in a new tab)</span>
                  </a>
                </li>
              </ul>
            </div>
          </div>
        </div>
        <div className="mt-16 flex flex-col gap-3 border-t border-white/10 pt-6 text-xs text-white/45 sm:flex-row sm:items-center sm:justify-between">
          <Wordmark className="text-base text-white/70" />
          <p>Every story links to its original source. Sample content is labelled until the live feed is connected.</p>
        </div>
      </div>
    </footer>
  );
}

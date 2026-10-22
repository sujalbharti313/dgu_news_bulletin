import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { StaticOrb } from "@/components/hero/StaticOrb";

export default function NotFound() {
  return (
    <main id="main" className="relative flex min-h-[100svh] items-center overflow-hidden bg-night text-white">
      <StaticOrb className="absolute -right-32 top-1/2 w-[640px] -translate-y-1/2 opacity-60" />
      <div className="shell relative">
        <p className="eyebrow text-lilac">404 · Signal lost</p>
        <h1 className="display mt-5 text-[clamp(3rem,9vw,7rem)]">
          This page isn’t
          <br />
          <span className="text-lilac">in the archive.</span>
        </h1>
        <p className="mt-6 max-w-md text-lg text-white/70">The story may have moved, or the link may be mistyped.</p>
        <div className="mt-9 flex flex-wrap gap-3">
          <Link href="/" className="inline-flex h-12 items-center gap-2 rounded-full bg-yellow px-6 font-semibold text-ink">
            <ArrowLeft className="size-4" aria-hidden /> Back to this week
          </Link>
          <Link href="/archive" className="inline-flex h-12 items-center rounded-full border border-white/25 px-6">
            Search the archive
          </Link>
        </div>
      </div>
    </main>
  );
}

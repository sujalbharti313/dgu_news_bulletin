import Image from "next/image";
import Link from "next/link";

/**
 * AI//CAMPUS wordmark paired with the official DBS Global University logo.
 * The logo file is used exactly as supplied; it sits on a white plate so the
 * original purple artwork keeps its contrast on dark surfaces.
 */
export function Wordmark({ className = "" }: { className?: string }) {
  return (
    <span className={`display inline-flex items-baseline text-[1.35rem] tracking-[-0.04em] ${className}`}>
      AI<span className="mx-[0.06em] text-yellow">{"//"}</span>CAMPUS
    </span>
  );
}

export function UniversityLogo({ className = "", priority = false }: { className?: string; priority?: boolean }) {
  return (
    <span className={`inline-flex items-center rounded-[10px] bg-white px-2.5 py-1.5 shadow-[0_1px_0_rgb(0_0_0/0.04)] ${className}`}>
      <Image
        src="/brand/dbs-global-university-logo.png"
        alt="DBS Global University, Dehradun"
        width={500}
        height={144}
        priority={priority}
        className="h-7 w-auto sm:h-8 xl:h-9"
      />
    </span>
  );
}

export function BrandLockup() {
  return (
    <div className="flex items-center gap-3 sm:gap-4">
      <Link href="/" aria-label="AI//CAMPUS home" className="text-white">
        <Wordmark />
      </Link>
      <span aria-hidden className="hidden h-7 w-px bg-white/20 sm:block" />
      <span className="hidden sm:block">
        <UniversityLogo priority />
      </span>
    </div>
  );
}

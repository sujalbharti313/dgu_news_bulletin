export function ScrollIndicator({ href }: { href: string }) {
  return (
    <a href={href} className="group flex items-center gap-3 text-white/60 transition hover:text-white" aria-label="Scroll to this week’s stories">
      <span className="relative block h-12 w-px overflow-hidden bg-white/15">
        <span className="absolute inset-x-0 top-0 h-1/2 bg-yellow motion-safe:animate-[scroll-cue_2.4s_var(--ease-out)_infinite]" />
      </span>
      <span className="eyebrow">Scroll</span>
    </a>
  );
}

"use client";

import { ArrowUpRight, Search, X } from "lucide-react";
import Link from "next/link";
import { useModalDialog } from "@/components/ui/useModalDialog";
import { UniversityLogo, Wordmark } from "./BrandLockup";
import { navItems } from "./nav-items";

interface Props {
  open: boolean;
  onClose: () => void;
  onSearch: () => void;
}

export function MobileNavigation({ open, onClose, onSearch }: Props) {
  const ref = useModalDialog(open, onClose);

  return (
    <dialog
      ref={ref}
      id="mobile-navigation"
      aria-label="Site menu"
      className="m-0 h-dvh max-h-none w-full max-w-none bg-night p-0 text-white backdrop:bg-night/70 open:animate-[menu-in_0.45s_var(--ease-out)] lg:hidden"
    >
      <div className="hairline-grid-dark flex h-full flex-col">
        <div className="shell flex items-center justify-between py-4">
          <Wordmark className="text-white" />
          <button
            type="button"
            onClick={onClose}
            className="flex size-10 items-center justify-center rounded-full border border-white/20"
            aria-label="Close menu"
          >
            <X className="size-5" aria-hidden />
          </button>
        </div>
        <nav aria-label="Mobile" className="shell flex-1 overflow-y-auto pt-6">
          <ol className="divide-y divide-white/10 border-y border-white/10">
            {navItems.map((item, i) => (
              <li key={item.href}>
                <Link
                  href={item.href}
                  onClick={onClose}
                  className="group flex items-baseline justify-between py-4 text-[1.9rem] font-semibold tracking-tight display"
                >
                  <span>
                    <span className="eyebrow mr-4 align-middle text-white/40">{String(i + 1).padStart(2, "0")}</span>
                    {item.label}
                  </span>
                  <ArrowUpRight className="size-5 text-yellow opacity-60 transition group-hover:opacity-100" aria-hidden />
                </Link>
              </li>
            ))}
          </ol>
          <button
            type="button"
            onClick={() => {
              onClose();
              onSearch();
            }}
            className="mt-8 flex w-full items-center gap-3 rounded-2xl border border-white/15 bg-white/5 px-4 py-4 text-left text-white/80"
          >
            <Search className="size-5" aria-hidden />
            Search the archive
          </button>
        </nav>
        <div className="shell flex items-center justify-between gap-4 py-6">
          <UniversityLogo />
          <p className="max-w-[12rem] text-right text-xs text-white/50">Designed for the DBS Global University community</p>
        </div>
      </div>
    </dialog>
  );
}

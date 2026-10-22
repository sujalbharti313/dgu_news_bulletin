"use client";

import { Menu, Search } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import type { Story } from "@/lib/types";
import { BrandLockup } from "./BrandLockup";
import { MobileNavigation } from "./MobileNavigation";
import { navItems } from "./nav-items";
import { SearchDialog } from "./SearchDialog";

export function SiteHeader({ searchIndex }: { searchIndex: Story[] }) {
  const [scrolled, setScrolled] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 24);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = target?.closest("input, textarea, [contenteditable=true]");
      if ((e.key === "k" && (e.metaKey || e.ctrlKey)) || (e.key === "/" && !typing)) {
        e.preventDefault();
        setSearchOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const solid = scrolled || pathname !== "/";

  return (
    <>
    <header
      className={`fixed inset-x-0 top-0 z-50 transition-[background-color,box-shadow,padding] duration-500 ease-[var(--ease-out)] ${
        solid
          ? "bg-night/80 py-2.5 shadow-[0_10px_40px_-20px_rgb(0_0_0/0.6)] backdrop-blur-xl"
          : "bg-transparent py-4 sm:py-5"
      }`}
    >
      <div className="shell flex items-center justify-between gap-6">
        <BrandLockup />

        <nav aria-label="Primary" className="hidden lg:block">
          <ul className="flex items-center gap-1 rounded-full border border-white/10 bg-white/[0.04] p-1 backdrop-blur-md">
            {navItems.map((item) => {
              const active = item.href === pathname;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={`block rounded-full px-3.5 py-1.5 text-[0.82rem] font-medium transition-colors xl:px-4 ${
                      active ? "bg-white text-ink" : "text-white/80 hover:bg-white/10 hover:text-white"
                    }`}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setSearchOpen(true)}
            className="group flex h-10 items-center gap-2 rounded-full border border-white/15 bg-white/[0.06] px-3 text-white/85 transition hover:border-white/30 hover:text-white sm:px-4"
            aria-label="Search stories"
          >
            <Search className="size-4" aria-hidden />
            <span className="hidden text-sm sm:inline">Search</span>
            <kbd className="hidden rounded border border-white/20 px-1.5 font-mono text-[0.65rem] text-white/60 xl:inline">/</kbd>
          </button>
          <button
            type="button"
            onClick={() => setMenuOpen(true)}
            className="flex size-10 items-center justify-center rounded-full bg-yellow text-ink lg:hidden"
            aria-label="Open menu"
            aria-expanded={menuOpen}
            aria-controls="mobile-navigation"
          >
            <Menu className="size-5" aria-hidden />
          </button>
        </div>
      </div>
    </header>
    <MobileNavigation open={menuOpen} onClose={() => setMenuOpen(false)} onSearch={() => setSearchOpen(true)} />
    <SearchDialog open={searchOpen} onClose={() => setSearchOpen(false)} stories={searchIndex} />
    </>
  );
}

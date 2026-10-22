"use client";

import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { StaticOrb } from "./StaticOrb";

const IntelligenceScene = dynamic(() => import("./IntelligenceScene"), { ssr: false });

function supportsWebGL() {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch {
    return false;
  }
}

/**
 * Chooses between the live 3D scene and the static orb, and pauses rendering
 * whenever the hero is off-screen or the reader prefers reduced motion.
 */
export function HeroVisual() {
  const container = useRef<HTMLDivElement>(null);
  const [capable, setCapable] = useState<boolean | null>(null);
  const [ready, setReady] = useState(false);
  const [visible, setVisible] = useState(true);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    setCapable(supportsWebGL());
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const small = window.matchMedia("(max-width: 767px)");
    const sync = () => {
      setReducedMotion(motion.matches);
      setCompact(small.matches);
    };
    sync();
    motion.addEventListener("change", sync);
    small.addEventListener("change", sync);
    return () => {
      motion.removeEventListener("change", sync);
      small.removeEventListener("change", sync);
    };
  }, []);

  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const io = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { threshold: 0 });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  return (
    <div ref={container} className="pointer-events-none absolute inset-0" aria-hidden>
      <div
        className={`absolute inset-0 flex items-center justify-center transition-opacity duration-1000 md:justify-end md:pr-[4vw] ${
          ready ? "opacity-0" : "opacity-100"
        }`}
      >
        <StaticOrb className="w-[min(88vw,600px)] max-md:-mt-[52vh] md:mt-[-4vh]" />
      </div>
      {capable && (
        <div
          className={`absolute inset-0 transition-opacity duration-[1400ms] max-md:bottom-auto max-md:h-[44%] md:left-[34%] ${ready ? "opacity-100" : "opacity-0"}`}
        >
          <IntelligenceScene compact={compact} animate={visible && !reducedMotion} onReady={() => setReady(true)} />
        </div>
      )}
    </div>
  );
}

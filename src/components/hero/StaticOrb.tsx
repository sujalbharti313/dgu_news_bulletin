/**
 * Non-WebGL rendition of the intelligence orb. Shown while the 3D scene
 * loads, when WebGL is unavailable, and as the server-rendered first paint.
 */
export function StaticOrb({ className = "" }: { className?: string }) {
  const nodes = Array.from({ length: 42 }, (_, i) => {
    const angle = i * 2.399963; // golden angle
    const r = 120 * Math.sqrt((i + 0.5) / 42);
    return { x: 200 + Math.cos(angle) * r, y: 200 + Math.sin(angle) * r, accent: i % 11 === 4 };
  });
  return (
    <svg viewBox="0 0 400 400" className={className} aria-hidden>
      <defs>
        <radialGradient id="orb-core" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stopColor="#fff3b0" stopOpacity="0.95" />
          <stop offset="18%" stopColor="#e7b6f0" stopOpacity="0.8" />
          <stop offset="55%" stopColor="#65176a" stopOpacity="0.45" />
          <stop offset="100%" stopColor="#170a1b" stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="200" cy="200" r="150" fill="url(#orb-core)" style={{ transformOrigin: "center", animation: "orb-breathe 7s ease-in-out infinite" }} />
      <g fill="none" strokeWidth="0.8">
        <ellipse cx="200" cy="200" rx="185" ry="52" stroke="#ffde00" strokeOpacity="0.35" transform="rotate(-18 200 200)" />
        <ellipse cx="200" cy="200" rx="170" ry="70" stroke="#c99ad3" strokeOpacity="0.25" transform="rotate(28 200 200)" />
      </g>
      <g>
        {nodes.map((n, i) => (
          <circle key={i} cx={n.x.toFixed(2)} cy={n.y.toFixed(2)} r={n.accent ? 2.6 : 1.6} fill={n.accent ? "#ffde00" : "#e7d2ee"} opacity={n.accent ? 1 : 0.7} />
        ))}
      </g>
    </svg>
  );
}

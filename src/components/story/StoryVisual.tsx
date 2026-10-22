import type { Topic } from "@/lib/types";

/**
 * Original procedural artwork for stories without a verified source image.
 * Each topic has its own motif; the slug seeds the variation so no two
 * stories look identical, and nothing pretends to be a photograph.
 */

function seeded(slug: string) {
  let h = 2166136261;
  for (let i = 0; i < slug.length; i++) h = Math.imul(h ^ slug.charCodeAt(i), 16777619);
  return () => {
    h = Math.imul(h ^ (h >>> 15), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    return ((h ^= h >>> 16) >>> 0) / 4294967296;
  };
}

const f = (n: number) => n.toFixed(1);

function Motif({ topic, rand }: { topic: Topic; rand: () => number }) {
  switch (topic) {
    case "models": {
      // Concentric orbits around a dense core
      return (
        <g fill="none">
          {Array.from({ length: 7 }, (_, i) => (
            <ellipse
              key={i}
              cx="300"
              cy="200"
              rx={40 + i * 34}
              ry={18 + i * 15}
              stroke={i === 3 ? "#ffde00" : "#d9c4e0"}
              strokeOpacity={i === 3 ? 0.8 : 0.22 + i * 0.03}
              transform={`rotate(${f(-20 + rand() * 12)} 300 200)`}
            />
          ))}
          <circle cx="300" cy="200" r="16" fill="#ffde00" />
          <circle cx="300" cy="200" r="46" fill="#d9c4e0" fillOpacity="0.12" />
        </g>
      );
    }
    case "research": {
      // A dot lattice with one highlighted path of inference
      const dots = [];
      const path: string[] = [];
      for (let x = 0; x < 15; x++) {
        for (let y = 0; y < 9; y++) {
          const cx = 40 + x * 38;
          const cy = 45 + y * 39;
          dots.push(<circle key={`${x}-${y}`} cx={cx} cy={cy} r="1.8" fill="#d9c4e0" fillOpacity="0.35" />);
        }
        path.push(`${40 + x * 38},${f(45 + Math.round(rand() * 8) * 39)}`);
      }
      return (
        <g>
          {dots}
          <polyline points={path.join(" ")} fill="none" stroke="#ffde00" strokeWidth="2" strokeLinejoin="round" />
          {path.map((p, i) => {
            const [cx, cy] = p.split(",");
            return <circle key={i} cx={cx} cy={cy} r="4" fill="#170a1b" stroke="#ffde00" strokeWidth="1.5" />;
          })}
        </g>
      );
    }
    case "tools": {
      // A tool-calling graph: hub, tools and return paths
      const nodes = Array.from({ length: 8 }, (_, i) => {
        const a = (i / 8) * Math.PI * 2 + rand() * 0.3;
        const r = 110 + rand() * 50;
        return { x: 300 + Math.cos(a) * r * 1.5, y: 200 + Math.sin(a) * r * 0.85 };
      });
      return (
        <g>
          {nodes.map((n, i) => (
            <line key={i} x1="300" y1="200" x2={f(n.x)} y2={f(n.y)} stroke="#d9c4e0" strokeOpacity="0.35" strokeDasharray={i % 2 ? "4 6" : undefined} />
          ))}
          {nodes.map((n, i) => (
            <rect key={i} x={f(n.x - 14)} y={f(n.y - 14)} width="28" height="28" rx="8" fill="#241029" stroke={i === 2 ? "#ffde00" : "#d9c4e0"} strokeOpacity={i === 2 ? 1 : 0.5} />
          ))}
          <rect x="268" y="168" width="64" height="64" rx="18" fill="#65176a" stroke="#ffde00" />
        </g>
      );
    }
    case "india": {
      // Radiating language waves
      return (
        <g fill="none">
          {Array.from({ length: 12 }, (_, i) => {
            const amp = 10 + rand() * 26;
            const y = 60 + i * 26;
            let d = `M0 ${y}`;
            for (let x = 0; x <= 600; x += 30) d += ` Q ${x + 15} ${f(y + (x % 60 ? amp : -amp))} ${x + 30} ${y}`;
            return <path key={i} d={d} stroke={i === 6 ? "#ffde00" : "#d9c4e0"} strokeOpacity={i === 6 ? 0.9 : 0.25} />;
          })}
        </g>
      );
    }
    case "policy": {
      // Stacked governance layers
      return (
        <g>
          {Array.from({ length: 6 }, (_, i) => (
            <path
              key={i}
              d={`M ${150 + i * 6} ${250 - i * 28} L 300 ${f(310 - i * 28)} L ${450 - i * 6} ${250 - i * 28} L 300 ${f(190 - i * 28)} Z`}
              fill={i === 5 ? "#ffde00" : "#d9c4e0"}
              fillOpacity={i === 5 ? 0.85 : 0.06 + i * 0.03}
              stroke="#d9c4e0"
              strokeOpacity="0.4"
            />
          ))}
        </g>
      );
    }
    case "careers":
    case "campus":
    default: {
      // Ascending signal bars
      return (
        <g>
          {Array.from({ length: 18 }, (_, i) => {
            const h = 30 + i * 12 + rand() * 40;
            return (
              <rect key={i} x={60 + i * 27} y={f(360 - h)} width="12" height={f(h)} rx="6" fill={i === 14 ? "#ffde00" : "#d9c4e0"} fillOpacity={i === 14 ? 1 : 0.18 + i * 0.02} />
            );
          })}
        </g>
      );
    }
  }
}

export function StoryVisual({ slug, topic, className = "" }: { slug: string; topic: Topic; className?: string }) {
  const rand = seeded(slug);
  const gx = 30 + rand() * 40;
  return (
    <svg viewBox="0 0 600 400" preserveAspectRatio="xMidYMid slice" className={className} aria-hidden focusable="false">
      <defs>
        <radialGradient id={`bg-${slug}`} cx={`${f(gx)}%`} cy="40%" r="80%">
          <stop offset="0%" stopColor="#65176a" />
          <stop offset="55%" stopColor="#3a1a45" />
          <stop offset="100%" stopColor="#170a1b" />
        </radialGradient>
      </defs>
      <rect width="600" height="400" fill={`url(#bg-${slug})`} />
      <Motif topic={topic} rand={rand} />
    </svg>
  );
}

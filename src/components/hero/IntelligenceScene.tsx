"use client";

import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";

/**
 * The AI//CAMPUS signature scene: a neural orb of connected nodes around a
 * luminous core, wrapped in tilted orbital paths, with sparse signal pulses
 * travelling along the network. Everything is procedural; there are no
 * textures to download.
 */

const PURPLE = new THREE.Color("#b46ac0");
const LILAC = new THREE.Color("#e7d2ee");
const YELLOW = new THREE.Color("#ffde00");
const DEEP = new THREE.Color("#65176a");

// A single soft light point at the heart of the orb
const GLOW_POSITION = new Float32Array([0, 0, 0]);
const GLOW_COLOR = new Float32Array(new THREE.Color("#ffe98a").toArray());
const GLOW_SIZE = new Float32Array([1]);

/** Small seeded PRNG so geometry is identical on every load. */
function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Network {
  nodes: Float32Array;
  nodeColors: Float32Array;
  nodeSizes: Float32Array;
  edges: Float32Array;
  edgeColors: Float32Array;
  edgePairs: [number, number][];
}

function buildNetwork(count: number, radius: number): Network {
  const rand = rng(7);
  const pts: THREE.Vector3[] = [];
  const golden = Math.PI * (3 - Math.sqrt(5));
  for (let i = 0; i < count; i++) {
    const y = 1 - (i / (count - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const theta = golden * i;
    const jitter = radius * (0.86 + rand() * 0.24);
    pts.push(new THREE.Vector3(Math.cos(theta) * r * jitter, y * jitter, Math.sin(theta) * r * jitter));
  }

  const nodes = new Float32Array(count * 3);
  const nodeColors = new Float32Array(count * 3);
  const nodeSizes = new Float32Array(count);
  pts.forEach((p, i) => {
    p.toArray(nodes, i * 3);
    const accent = rand() < 0.07;
    (accent ? YELLOW : rand() < 0.5 ? LILAC : PURPLE).toArray(nodeColors, i * 3);
    nodeSizes[i] = accent ? 2.2 : 0.8 + rand() * 1.2;
  });

  // Connect each node to its nearest neighbours.
  const seen = new Set<string>();
  const edgePairs: [number, number][] = [];
  for (let i = 0; i < count; i++) {
    const nearest = pts
      .map((p, j) => ({ j, d: p.distanceToSquared(pts[i]) }))
      .filter(({ j }) => j !== i)
      .sort((a, b) => a.d - b.d)
      .slice(0, 3);
    for (const { j } of nearest) {
      const key = i < j ? `${i}-${j}` : `${j}-${i}`;
      if (seen.has(key)) continue;
      seen.add(key);
      edgePairs.push([i, j]);
    }
  }
  const edges = new Float32Array(edgePairs.length * 6);
  const edgeColors = new Float32Array(edgePairs.length * 6);
  edgePairs.forEach(([a, b], k) => {
    pts[a].toArray(edges, k * 6);
    pts[b].toArray(edges, k * 6 + 3);
    const c = PURPLE.clone().lerp(LILAC, rand() * 0.5);
    c.toArray(edgeColors, k * 6);
    c.toArray(edgeColors, k * 6 + 3);
  });

  return { nodes, nodeColors, nodeSizes, edges, edgeColors, edgePairs };
}

const pointVertex = /* glsl */ `
  attribute float size;
  attribute vec3 color;
  varying vec3 vColor;
  uniform float uPixelRatio;
  uniform float uScale;
  void main() {
    vColor = color;
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_PointSize = size * uScale * uPixelRatio * (8.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
  }
`;

const pointFragment = /* glsl */ `
  varying vec3 vColor;
  uniform float uOpacity;
  void main() {
    float d = length(gl_PointCoord - 0.5);
    float core = smoothstep(0.5, 0.0, d);
    float glow = pow(core, 2.4);
    gl_FragColor = vec4(vColor, glow * uOpacity);
  }
`;

const coreVertex = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vNormal = normalize(normalMatrix * normal);
    vView = normalize(-mv.xyz);
    gl_Position = projectionMatrix * mv;
  }
`;

const coreFragment = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  uniform vec3 uInner;
  uniform vec3 uRim;
  uniform float uTime;
  void main() {
    float fres = pow(1.0 - max(dot(vNormal, vView), 0.0), 2.6);
    float pulse = 0.88 + 0.12 * sin(uTime * 0.9);
    vec3 col = mix(uInner * 0.6, uRim, fres) * pulse;
    gl_FragColor = vec4(col, 0.08 + fres * 0.8);
  }
`;

function usePointMaterial(scale: number, opacity = 1) {
  const gl = useThree((s) => s.gl);
  return useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: pointVertex,
        fragmentShader: pointFragment,
        uniforms: {
          uPixelRatio: { value: gl.getPixelRatio() },
          uScale: { value: scale },
          uOpacity: { value: opacity },
        },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    [gl, scale, opacity],
  );
}

function NeuralOrb({ compact }: { compact: boolean }) {
  const network = useMemo(() => buildNetwork(compact ? 150 : 240, 1.65), [compact]);
  const nodeMaterial = usePointMaterial(compact ? 7 : 8);
  const pulseMaterial = usePointMaterial(compact ? 14 : 16);
  const glowMaterial = usePointMaterial(compact ? 48 : 60, 0.9);
  const coreMaterial = useMemo(
    () =>
      new THREE.ShaderMaterial({
        vertexShader: coreVertex,
        fragmentShader: coreFragment,
        uniforms: { uInner: { value: DEEP }, uRim: { value: new THREE.Color("#f0c8ff") }, uTime: { value: 0 } },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      }),
    [],
  );

  // Signal pulses travelling along random edges.
  const pulseCount = compact ? 8 : 14;
  const pulses = useMemo(() => {
    const rand = rng(42);
    return Array.from({ length: pulseCount }, () => ({
      edge: Math.floor(rand() * network.edgePairs.length),
      t: rand(),
      speed: 0.25 + rand() * 0.35,
    }));
  }, [network, pulseCount]);
  const pulseGeometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(pulseCount * 3), 3));
    const colors = new Float32Array(pulseCount * 3);
    const sizes = new Float32Array(pulseCount);
    for (let i = 0; i < pulseCount; i++) {
      (i % 3 === 0 ? YELLOW : LILAC).toArray(colors, i * 3);
      sizes[i] = i % 3 === 0 ? 1.6 : 1.1;
    }
    g.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    g.setAttribute("size", new THREE.BufferAttribute(sizes, 1));
    return g;
  }, [pulseCount]);

  const pulseRand = useMemo(() => rng(99), []);
  const a = useMemo(() => new THREE.Vector3(), []);
  const b = useMemo(() => new THREE.Vector3(), []);

  useFrame((state, delta) => {
    coreMaterial.uniforms.uTime.value = state.clock.elapsedTime;
    const pos = pulseGeometry.attributes.position as THREE.BufferAttribute;
    pulses.forEach((p, i) => {
      p.t += delta * p.speed;
      if (p.t >= 1) {
        p.t = 0;
        p.edge = Math.floor(pulseRand() * network.edgePairs.length);
      }
      const [ia, ib] = network.edgePairs[p.edge];
      a.fromArray(network.nodes, ia * 3);
      b.fromArray(network.nodes, ib * 3);
      a.lerp(b, p.t);
      pos.setXYZ(i, a.x, a.y, a.z);
    });
    pos.needsUpdate = true;
  });

  return (
    <group>
      <mesh material={coreMaterial}>
        <icosahedronGeometry args={[0.62, 5]} />
      </mesh>
      <points material={glowMaterial}>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[GLOW_POSITION, 3]} />
          <bufferAttribute attach="attributes-color" args={[GLOW_COLOR, 3]} />
          <bufferAttribute attach="attributes-size" args={[GLOW_SIZE, 1]} />
        </bufferGeometry>
      </points>
      <lineSegments>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[network.edges, 3]} />
          <bufferAttribute attach="attributes-color" args={[network.edgeColors, 3]} />
        </bufferGeometry>
        <lineBasicMaterial vertexColors transparent opacity={0.28} blending={THREE.AdditiveBlending} depthWrite={false} />
      </lineSegments>
      <points material={nodeMaterial}>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" args={[network.nodes, 3]} />
          <bufferAttribute attach="attributes-color" args={[network.nodeColors, 3]} />
          <bufferAttribute attach="attributes-size" args={[network.nodeSizes, 1]} />
        </bufferGeometry>
      </points>
      <points geometry={pulseGeometry} material={pulseMaterial} />
    </group>
  );
}

function Orbit({ radius, tilt, speed, accent }: { radius: number; tilt: [number, number, number]; speed: number; accent?: boolean }) {
  const satellite = useRef<THREE.Mesh>(null);
  const ringGeometry = useMemo(() => {
    const curve = new THREE.EllipseCurve(0, 0, radius, radius, 0, Math.PI * 2);
    return new THREE.BufferGeometry().setFromPoints(curve.getPoints(160).map((p) => new THREE.Vector3(p.x, 0, p.y)));
  }, [radius]);

  useFrame((state) => {
    if (!satellite.current) return;
    const t = state.clock.elapsedTime * speed;
    satellite.current.position.set(Math.cos(t) * radius, 0, Math.sin(t) * radius);
  });

  return (
    <group rotation={tilt}>
      <lineLoop geometry={ringGeometry}>
        <lineBasicMaterial color={accent ? "#ffde00" : "#c99ad3"} transparent opacity={accent ? 0.35 : 0.18} />
      </lineLoop>
      <mesh ref={satellite}>
        <sphereGeometry args={[accent ? 0.05 : 0.035, 16, 16]} />
        <meshBasicMaterial color={accent ? "#ffde00" : "#f3e6f7"} />
      </mesh>
    </group>
  );
}

function Dust({ count }: { count: number }) {
  const ref = useRef<THREE.Points>(null);
  const material = usePointMaterial(3, 0.55);
  const { positions, colors, sizes } = useMemo(() => {
    const rand = rng(3);
    const positions = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const sizes = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      positions.set([(rand() - 0.5) * 16, (rand() - 0.5) * 10, (rand() - 0.5) * 8 - 2], i * 3);
      (rand() < 0.04 ? YELLOW : LILAC).toArray(colors, i * 3);
      sizes[i] = 0.6 + rand() * 1.6;
    }
    return { positions, colors, sizes };
  }, [count]);

  useFrame((_, delta) => {
    if (ref.current) ref.current.rotation.y += delta * 0.008;
  });

  return (
    <points ref={ref} material={material}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        <bufferAttribute attach="attributes-color" args={[colors, 3]} />
        <bufferAttribute attach="attributes-size" args={[sizes, 1]} />
      </bufferGeometry>
    </points>
  );
}

/** Pointer- and scroll-aware rig. Pointer follow is disabled on touch devices. */
function Rig({ children, compact }: { children: React.ReactNode; compact: boolean }) {
  const group = useRef<THREE.Group>(null);
  const pointer = useRef({ x: 0, y: 0 });

  useEffect(() => {
    if (!window.matchMedia("(pointer: fine)").matches) return;
    const onMove = (e: PointerEvent) => {
      pointer.current.x = (e.clientX / window.innerWidth) * 2 - 1;
      pointer.current.y = (e.clientY / window.innerHeight) * 2 - 1;
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    return () => window.removeEventListener("pointermove", onMove);
  }, []);

  useFrame((state, delta) => {
    const g = group.current;
    if (!g) return;
    const scroll = Math.min(window.scrollY / window.innerHeight, 1);
    const ease = 1 - Math.pow(0.002, delta);
    g.rotation.y += delta * 0.06;
    g.rotation.x = THREE.MathUtils.lerp(g.rotation.x, pointer.current.y * 0.22 + 0.25, ease);
    g.rotation.z = THREE.MathUtils.lerp(g.rotation.z, -pointer.current.x * 0.12, ease);
    g.position.y = THREE.MathUtils.lerp(g.position.y, scroll * 1.1, ease);
    const s = (compact ? 0.7 : 0.9) * (1 - scroll * 0.18);
    g.scale.setScalar(THREE.MathUtils.lerp(g.scale.x, s, ease));
    state.camera.position.x = THREE.MathUtils.lerp(state.camera.position.x, pointer.current.x * 0.35, ease);
    state.camera.lookAt(0, 0, 0);
  });

  return <group ref={group}>{children}</group>;
}

export interface IntelligenceSceneProps {
  compact: boolean;
  animate: boolean;
  onReady: () => void;
}

export default function IntelligenceScene({ compact, animate, onReady }: IntelligenceSceneProps) {
  return (
    <Canvas
      dpr={[1, compact ? 1.5 : 1.75]}
      camera={{ position: [0, 0, 6.2], fov: 42 }}
      gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
      frameloop={animate ? "always" : "demand"}
      onCreated={({ gl }) => {
        gl.setClearColor(0x000000, 0);
        onReady();
      }}
      aria-hidden
      style={{ pointerEvents: "none" }}
    >
      <Dust count={compact ? 220 : 420} />
      <Rig compact={compact}>
        <NeuralOrb compact={compact} />
        <Orbit radius={2.35} tilt={[1.25, 0, 0.35]} speed={0.22} accent />
        <Orbit radius={2.75} tilt={[1.0, 0.4, -0.5]} speed={-0.14} />
        <Orbit radius={3.2} tilt={[1.45, -0.3, 0.1]} speed={0.09} />
      </Rig>
    </Canvas>
  );
}

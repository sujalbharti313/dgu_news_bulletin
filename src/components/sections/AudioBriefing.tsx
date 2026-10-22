"use client";

import { ChevronDown, Pause, Play, VolumeX } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { formatTime } from "@/lib/format";
import type { Briefing } from "@/lib/types";

const BAR_COUNT = 72;

function waveform(seed: string) {
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return Array.from({ length: BAR_COUNT }, (_, i) => {
    h = (h * 1664525 + 1013904223) >>> 0;
    const envelope = Math.sin((i / BAR_COUNT) * Math.PI) * 0.55 + 0.35;
    return Math.max(0.12, envelope * (0.45 + (h / 4294967296) * 0.55));
  });
}

export function AudioBriefing({ briefing }: { briefing: Briefing }) {
  const audio = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState(briefing.durationSeconds ?? 0);
  const [failed, setFailed] = useState(false);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const transcriptId = useId();
  const bars = useMemo(() => waveform(briefing.editionId), [briefing.editionId]);
  const available = !!briefing.audioUrl && !failed;
  const progress = duration > 0 ? current / duration : 0;

  useEffect(() => {
    const el = audio.current;
    if (!el) return;
    const onTime = () => setCurrent(el.currentTime);
    const onMeta = () => setDuration(el.duration);
    const onEnd = () => setPlaying(false);
    const onError = () => {
      setFailed(true);
      setPlaying(false);
    };
    el.addEventListener("timeupdate", onTime);
    el.addEventListener("loadedmetadata", onMeta);
    el.addEventListener("ended", onEnd);
    el.addEventListener("error", onError);
    return () => {
      el.removeEventListener("timeupdate", onTime);
      el.removeEventListener("loadedmetadata", onMeta);
      el.removeEventListener("ended", onEnd);
      el.removeEventListener("error", onError);
    };
  }, []);

  const toggle = async () => {
    const el = audio.current;
    if (!el || !available) return;
    if (el.paused) {
      try {
        await el.play();
        setPlaying(true);
      } catch {
        setFailed(true);
      }
    } else {
      el.pause();
      setPlaying(false);
    }
  };

  const seek = (value: number) => {
    if (audio.current && duration) {
      audio.current.currentTime = value;
      setCurrent(value);
    }
  };

  return (
    <section id="briefing" aria-labelledby="briefing-title" className="relative overflow-hidden bg-night py-24 text-white sm:py-32">
      <div className="absolute inset-0 bg-[radial-gradient(ellipse_50%_80%_at_85%_50%,rgb(101_23_106/0.7),transparent_70%)]" aria-hidden />
      <div className="shell relative grid gap-12 lg:grid-cols-12 lg:items-center">
        <div className="lg:col-span-5">
          <p className="eyebrow flex items-center gap-3 text-lilac">
            <span className="text-white/40">07</span>
            <span className="h-px w-8 bg-white/25" aria-hidden />
            The 60-second briefing
          </p>
          <h2 id="briefing-title" className="display mt-5 text-[clamp(2.5rem,6vw,5rem)]">
            The week,
            <br />
            <span className="text-lilac">in one minute.</span>
          </h2>
          <p className="mt-6 max-w-md text-lg leading-relaxed text-white/70">
            A short audio summary of each edition for your walk to class, with a full transcript.
          </p>
        </div>

        <div className="rounded-[var(--radius-lg)] border border-white/12 bg-white/[0.04] p-6 backdrop-blur-md sm:p-9 lg:col-span-7">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p className="eyebrow text-white/50">Weekly edition</p>
              <p className="mt-2 text-xl font-semibold">{briefing.title}</p>
            </div>
            {briefing.provenance === "sample" && <span className="sample-tag text-yellow/90">Sample</span>}
          </div>

          {briefing.audioUrl && <audio ref={audio} src={briefing.audioUrl} preload="metadata" />}

          <div className="mt-8 flex items-center gap-5 sm:gap-7">
            <button
              type="button"
              onClick={toggle}
              disabled={!available}
              aria-label={playing ? "Pause briefing" : "Play briefing"}
              className="flex size-16 shrink-0 items-center justify-center rounded-full bg-yellow text-ink shadow-[0_0_0_10px_rgb(255_222_0/0.1)] transition hover:scale-105 disabled:cursor-not-allowed disabled:bg-white/15 disabled:text-white/50 disabled:shadow-none disabled:hover:scale-100 sm:size-20"
            >
              {playing ? <Pause className="size-7" aria-hidden /> : <Play className="ml-1 size-7" aria-hidden />}
            </button>

            <div className="relative h-20 flex-1" aria-hidden>
              <div className="flex h-full items-center gap-[3px]">
                {bars.map((h, i) => {
                  const played = available && i / BAR_COUNT < progress;
                  return (
                    <span
                      key={i}
                      className={`flex-1 rounded-full transition-colors duration-300 ${played ? "bg-yellow" : available ? "bg-white/40" : "bg-white/15"} ${
                        playing ? "motion-safe:animate-pulse" : ""
                      }`}
                      style={{ height: `${Math.round(h * 100)}%`, animationDelay: `${(i % 9) * 90}ms` }}
                    />
                  );
                })}
              </div>
            </div>
          </div>

          {available ? (
            <div className="mt-5 flex items-center gap-4 text-sm tabular-nums text-white/70">
              <span>{formatTime(current)}</span>
              <label className="sr-only" htmlFor="briefing-seek">
                Seek
              </label>
              <input
                id="briefing-seek"
                type="range"
                min={0}
                max={duration || 0}
                step={0.1}
                value={current}
                onChange={(e) => seek(Number(e.target.value))}
                className="h-1 flex-1 cursor-pointer accent-yellow"
              />
              <span>{formatTime(duration)}</span>
            </div>
          ) : (
            <p className="mt-6 flex items-center gap-2.5 rounded-xl bg-white/[0.05] px-4 py-3 text-sm text-white/70">
              <VolumeX className="size-4 shrink-0 text-lilac" aria-hidden />
              {failed
                ? "The audio file could not be loaded. The transcript is still available below."
                : "Audio isn’t available for this edition yet. Read the transcript instead."}
            </p>
          )}

          <div className="mt-6 border-t border-white/10 pt-5">
            <button
              type="button"
              aria-expanded={transcriptOpen}
              aria-controls={transcriptId}
              onClick={() => setTranscriptOpen((o) => !o)}
              className="flex w-full items-center justify-between text-left font-medium"
            >
              Transcript
              <ChevronDown className={`size-5 transition ${transcriptOpen ? "rotate-180" : ""}`} aria-hidden />
            </button>
            <div id={transcriptId} hidden={!transcriptOpen} className="mt-4 max-h-64 space-y-3 overflow-y-auto pr-2 leading-relaxed text-white/75">
              {briefing.transcript.map((p, i) => (
                <p key={i}>{p}</p>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

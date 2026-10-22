import type { Topic } from "./types";

export const topicLabels: Record<Topic, string> = {
  models: "Models",
  research: "Research",
  tools: "Tools & agents",
  policy: "Policy",
  india: "India × AI",
  careers: "Careers",
  campus: "Campus",
};

const dateFmt = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const monthFmt = new Intl.DateTimeFormat("en-IN", { month: "short", year: "numeric", timeZone: "UTC" });

export const formatDate = (iso: string) => dateFmt.format(new Date(`${iso}T00:00:00Z`));
export const formatMonth = (iso: string) => monthFmt.format(new Date(`${iso}T00:00:00Z`));
export const pad2 = (n: number) => String(n).padStart(2, "0");

export function formatTime(seconds: number) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  return `${m}:${pad2(Math.floor(seconds % 60))}`;
}

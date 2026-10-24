// Regenerates app/seed/data/*.json from the frontend's own data files so the seed matches the UI exactly.
// Run from backend/: node --experimental-strip-types scripts/export_frontend_fixtures.mjs
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const root = new URL("../../src/data/", import.meta.url);
const out = (name) => fileURLToPath(new URL(`../app/seed/data/${name}`, import.meta.url));
const write = (name, value) => writeFileSync(out(name), JSON.stringify(value, null, 2) + "\n");

const reference = await import(new URL("reference.ts", root));
const stories = await import(new URL("sample/stories.ts", root));
const campus = await import(new URL("sample/campus.ts", root));

write("reference.json", {
  tools: reference.tools,
  researchPapers: reference.researchPapers,
  indiaEcosystem: reference.indiaEcosystem,
  careerSignals: campus.careerSignals,
});
write("demo.json", {
  editions: stories.sampleEditions,
  stories: stories.sampleStories,
  briefing: campus.sampleBriefing,
  campusItems: campus.sampleCampusItems,
});

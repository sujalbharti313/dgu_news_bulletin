import type { Briefing, CampusItem, CareerSignal } from "@/lib/types";

/**
 * SAMPLE / EDUCATIONAL DATA.
 * Campus items are example formats only, not official DBS Global University
 * events. Career signals are general learning suggestions, not published
 * opportunities, and make no claims about hiring or salaries.
 */

export const sampleCampusItems: CampusItem[] = [
  {
    id: "campus-project",
    provenance: "sample",
    kind: "Project",
    title: "Student project spotlight",
    description: "Format for featuring a student-built AI project, with a demo link and what the team learned.",
  },
  {
    id: "campus-workshop",
    provenance: "sample",
    kind: "Workshop",
    title: "Hands-on workshop listing",
    description: "Format for an approved workshop: topic, level, prerequisites and how to register.",
  },
  {
    id: "campus-hackathon",
    provenance: "sample",
    kind: "Hackathon",
    title: "Hackathon or competition call",
    description: "Format for a verified competition, with eligibility, deadlines and the official rules link.",
  },
  {
    id: "campus-club",
    provenance: "sample",
    kind: "Club",
    title: "Club activity note",
    description: "Format for reading groups, build nights and club announcements submitted by student organisers.",
  },
];

export const careerSignals: CareerSignal[] = [
  {
    id: "skill-evals",
    kind: "skill",
    title: "Evaluating models on your own task",
    description: "Building a small, honest test set and measuring a model against it is useful in almost any AI role.",
    startWith: "Write 30 real examples from your project and score two models on them.",
    relatedTopics: ["models", "research"],
  },
  {
    id: "skill-retrieval",
    kind: "skill",
    title: "Retrieval and embeddings",
    description: "Most applied LLM products search over documents. Understanding chunking and vector search is foundational.",
    startWith: "Index your course PDFs and compare keyword search with embedding search.",
    relatedTopics: ["tools"],
  },
  {
    id: "project-local-language",
    kind: "project",
    title: "A tool for a local language",
    description: "Underserved languages are a place where a student project can be genuinely useful.",
    startWith: "Pick one task (transcription, translation, search) and one language you speak.",
    relatedTopics: ["india"],
  },
  {
    id: "project-agent-audit",
    kind: "project",
    title: "An agent with an audit log",
    description: "Agents that explain every action are easier to trust and debug.",
    startWith: "Build a minimal tool-calling loop that writes each step to a readable log.",
    relatedTopics: ["tools"],
  },
  {
    id: "oss-docs",
    kind: "open-source",
    title: "Documentation contributions",
    description: "Improving docs and examples in open-source AI libraries is a respected first contribution.",
    startWith: "Find an issue labelled “documentation” or “good first issue” in a library you already use.",
    relatedTopics: ["tools", "models"],
  },
  {
    id: "oss-datasets",
    kind: "open-source",
    title: "Dataset cards and cleaning",
    description: "Clear dataset documentation helps everyone. It also teaches you how data shapes models.",
    startWith: "Read three dataset cards on the Hugging Face Hub and note what is missing.",
    relatedTopics: ["research", "india"],
  },
];

export const sampleBriefing: Briefing = {
  editionId: "2026-w43",
  title: "Edition 03 · Smaller models, bigger reach",
  audioUrl: null,
  durationSeconds: null,
  provenance: "sample",
  transcript: [
    "Welcome to the AI//CAMPUS sixty-second briefing. This is a sample transcript used to preview the format.",
    "This week’s sample edition looks at compact open models you can run on a laptop, and why that changes how students prototype.",
    "We also cover browser-using agents and where they still fail, why open Indian-language speech data matters, and how to read a model card before trusting a model.",
    "Your one thing to try: write thirty real examples from your project and test two models against them.",
  ],
};

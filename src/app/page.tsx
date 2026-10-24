import { HeroSection } from "@/components/hero/HeroSection";
import { ArchiveSearch } from "@/components/sections/ArchiveSearch";
import { AudioBriefing } from "@/components/sections/AudioBriefing";
import { CampusAI } from "@/components/sections/CampusAI";
import { CareerSignal } from "@/components/sections/CareerSignal";
import { IndiaAISection } from "@/components/sections/IndiaAISection";
import { ResearchRadar } from "@/components/sections/ResearchRadar";
import { StudentImpact } from "@/components/sections/StudentImpact";
import { ToolRadar } from "@/components/sections/ToolRadar";
import { WeeklyArchivePreview } from "@/components/sections/WeeklyArchivePreview";
import { WeeklySignal } from "@/components/sections/WeeklySignal";
import { ErrorState } from "@/components/ui/States";
import { toolCategories } from "@/data/reference";
import {
  getCampusItems,
  getCareerSignals,
  getCurrentEdition,
  getEditions,
  getIndiaEcosystem,
  getLatestBriefing,
  getResearchPapers,
  getStories,
  getTools,
} from "@/lib/api";

export default async function HomePage() {
  const [current, editions, allStories, briefing, tools, papers, india, careers, campus] = await Promise.all([
    getCurrentEdition(),
    getEditions(),
    getStories(),
    getLatestBriefing(),
    getTools(),
    getResearchPapers(),
    getIndiaEcosystem(),
    getCareerSignals(),
    getCampusItems(),
  ]);

  return (
    <main id="main">
      <HeroSection edition={current.ok ? current.data.edition : null} storyCount={current.ok ? current.data.stories.length : 0} />

      {current.ok ? (
        <>
          <WeeklySignal edition={current.data.edition} stories={current.data.stories} />
          <StudentImpact stories={current.data.stories} />
        </>
      ) : (
        <div className="bg-night">
          <section id="signal" aria-label="The Signal" className="rounded-t-[clamp(28px,5vw,64px)] bg-soft py-24">
            <div className="shell">
              <ErrorState title="This week’s stories could not load" message={current.error} />
            </div>
          </section>
        </div>
      )}

      {tools.ok && tools.data.length > 0 && <ToolRadar tools={tools.data} categories={toolCategories} />}
      {papers.ok && papers.data.length > 0 && <ResearchRadar papers={papers.data} />}
      {india.ok && india.data.length > 0 && <IndiaAISection entries={india.data} />}
      {careers.ok && careers.data.length > 0 && <CareerSignal signals={careers.data} />}
      {briefing.ok && <AudioBriefing briefing={briefing.data} />}
      <ArchiveSearch stories={allStories.ok ? allStories.data : []} />
      {campus.ok && campus.data.length > 0 && <CampusAI items={campus.data} />}
      {editions.ok && allStories.ok && <WeeklyArchivePreview editions={editions.data} stories={allStories.data} />}
    </main>
  );
}

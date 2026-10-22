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
import { careerSignals, sampleCampusItems } from "@/data/sample/campus";
import { indiaEcosystem, researchPapers, toolCategories, tools } from "@/data/reference";
import { getCurrentEdition, getEditions, getLatestBriefing, getStories } from "@/lib/api";

export default async function HomePage() {
  const [current, editions, allStories, briefing] = await Promise.all([
    getCurrentEdition(),
    getEditions(),
    getStories(),
    getLatestBriefing(),
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

      <ToolRadar tools={tools} categories={toolCategories} />
      <ResearchRadar papers={researchPapers} />
      <IndiaAISection entries={indiaEcosystem} />
      <CareerSignal signals={careerSignals} />
      {briefing.ok && <AudioBriefing briefing={briefing.data} />}
      <ArchiveSearch stories={allStories.ok ? allStories.data : []} />
      <CampusAI items={sampleCampusItems} />
      {editions.ok && allStories.ok && <WeeklyArchivePreview editions={editions.data} stories={allStories.data} />}
    </main>
  );
}

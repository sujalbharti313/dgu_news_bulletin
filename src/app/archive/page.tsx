import type { Metadata } from "next";
import { Suspense } from "react";
import { WeeklyArchive } from "@/components/sections/WeeklyArchive";
import { SampleNotice } from "@/components/ui/SampleNotice";
import { ErrorState, LoadingState } from "@/components/ui/States";
import { getEditions, getStories } from "@/lib/api";

export const metadata: Metadata = {
  title: "Weekly archive",
  description: "Browse and search every AI//CAMPUS edition by topic, week and date.",
};

export default async function ArchivePage() {
  const [editions, stories] = await Promise.all([getEditions(), getStories()]);

  return (
    <main id="main">
      <div className="relative overflow-hidden bg-night pb-28 pt-36 text-white">
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_60%_80%_at_80%_20%,#65176a,transparent_70%)]" aria-hidden />
        <div className="hairline-grid-dark absolute inset-0" aria-hidden />
        <div className="shell relative">
          <p className="eyebrow text-lilac">Weekly archive</p>
          <h1 className="display mt-5 text-[clamp(3rem,9vw,7.5rem)]">
            Every signal,
            <br />
            <span className="text-lilac">on the record.</span>
          </h1>
        </div>
      </div>
      <div className="-mt-12 rounded-t-[clamp(28px,5vw,64px)] bg-soft pb-24 pt-14 relative">
        <div className="shell">
          {!editions.ok || !stories.ok ? (
            <ErrorState title="The archive could not load" message={!editions.ok ? editions.error : !stories.ok ? stories.error : undefined} />
          ) : (
            <>
              {stories.source === "sample" && (
                <div className="mb-10 max-w-3xl">
                  <SampleNotice />
                </div>
              )}
              <Suspense fallback={<LoadingState label="Loading the archive" />}>
                <WeeklyArchive editions={editions.data} stories={stories.data} />
              </Suspense>
            </>
          )}
        </div>
      </div>
    </main>
  );
}

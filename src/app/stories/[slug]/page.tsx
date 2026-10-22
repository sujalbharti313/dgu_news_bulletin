import { ArrowLeft, Compass, GraduationCap, Hammer, Lightbulb } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { SourceLink } from "@/components/story/SourceLink";
import { StoryMeta, TopicPill } from "@/components/story/StoryMeta";
import { StoryRow } from "@/components/story/StoryRow";
import { StoryVisual } from "@/components/story/StoryVisual";
import { SampleNotice } from "@/components/ui/SampleNotice";
import { ErrorState } from "@/components/ui/States";
import { getStories, getStory } from "@/lib/api";
import { isLiveApiConfigured } from "@/lib/api/config";

interface Params {
  params: Promise<{ slug: string }>;
}

export async function generateStaticParams() {
  if (isLiveApiConfigured) return [];
  const stories = await getStories();
  return stories.ok ? stories.data.map((s) => ({ slug: s.slug })) : [];
}

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { slug } = await params;
  const result = await getStory(slug);
  if (!result.ok || !result.data) return { title: "Story not found" };
  const story = result.data;
  return {
    title: story.headline,
    description: story.dek,
    openGraph: { type: "article", title: story.headline, description: story.dek, publishedTime: story.publishedAt },
  };
}

export default async function StoryPage({ params }: Params) {
  const { slug } = await params;
  const result = await getStory(slug);

  if (!result.ok) {
    return (
      <main id="main" className="bg-soft pb-24 pt-32">
        <div className="shell">
          <ErrorState title="This story could not load" message={result.error} />
        </div>
      </main>
    );
  }
  const story = result.data;
  if (!story) notFound();

  const all = await getStories();
  const related = all.ok
    ? all.data.filter((s) => s.slug !== story.slug && (s.topic === story.topic || s.editionId === story.editionId)).slice(0, 3)
    : [];

  const impact = [
    { icon: Lightbulb, label: "Why should a student care?", body: story.impact.whyItMatters },
    { icon: Compass, label: "What to explore", body: story.impact.explore.join(" · ") },
    { icon: Hammer, label: "What you could build", body: story.impact.buildIdea },
    { icon: GraduationCap, label: "Relevant disciplines", body: story.impact.disciplines.join(" · ") },
  ];

  return (
    <main id="main">
      <div className="relative overflow-hidden bg-night pb-36 pt-32 text-white sm:pt-40">
        <StoryVisual slug={story.slug} topic={story.topic} className="absolute inset-0 h-full w-full opacity-50" />
        <div className="absolute inset-0 bg-gradient-to-b from-night/40 via-night/70 to-night" aria-hidden />
        <div className="shell relative">
          <Link href="/#signal" className="inline-flex items-center gap-2 text-sm text-white/70 transition hover:text-white">
            <ArrowLeft className="size-4" aria-hidden /> Back to The Signal
          </Link>
          <div className="mt-10 max-w-4xl">
            <TopicPill story={story} tone="dark" />
            <h1 className="display mt-6 text-[clamp(2.4rem,6vw,5rem)] leading-[0.98]">{story.headline}</h1>
            <p className="mt-6 max-w-2xl text-xl leading-relaxed text-white/75">{story.dek}</p>
            <div className="mt-8">
              <StoryMeta story={story} tone="dark" />
            </div>
          </div>
        </div>
      </div>

      <div className="relative -mt-20 rounded-t-[clamp(28px,5vw,64px)] bg-soft pb-24 pt-14 sm:pt-20">
        <div className="shell grid gap-14 lg:grid-cols-12">
          <article className="lg:col-span-7">
            {story.provenance === "sample" && (
              <div className="mb-10">
                <SampleNotice>
                  <strong className="text-ink">Sample story.</strong> This page demonstrates the AI//CAMPUS reading
                  experience. It does not report a specific real-world event and has no original article.
                </SampleNotice>
              </div>
            )}
            <div className="space-y-6 text-lg leading-[1.75] text-ink [&>p:first-child]:text-xl">
              {story.body.map((p, i) => (
                <p key={i}>{p}</p>
              ))}
            </div>
            <div className="mt-12 rounded-[var(--radius-md)] border border-line bg-white p-6">
              <p className="eyebrow text-purple">Original source</p>
              <div className="mt-3 text-ink">
                <SourceLink story={story} className="text-purple" />
              </div>
            </div>
          </article>

          <aside aria-labelledby="impact-heading" className="lg:col-span-5">
            <div className="rounded-[var(--radius-lg)] bg-purple p-7 text-white sm:p-9 lg:sticky lg:top-28">
              <p className="eyebrow text-yellow">Student impact</p>
              <h2 id="impact-heading" className="display mt-3 text-3xl">What this means for you</h2>
              <dl className="mt-8 space-y-7">
                {impact.map(({ icon: Icon, label, body }) => (
                  <div key={label}>
                    <dt className="flex items-center gap-2.5 font-semibold">
                      <Icon className="size-4 text-yellow" aria-hidden /> {label}
                    </dt>
                    <dd className="mt-2 leading-relaxed text-white/80">{body}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </aside>
        </div>

        {related.length > 0 && (
          <section aria-labelledby="related-title" className="shell mt-24">
            <h2 id="related-title" className="display border-b border-ink pb-4 text-3xl text-ink">
              Keep reading
            </h2>
            {related.map((s, i) => (
              <StoryRow key={s.slug} story={s} index={i + 1} />
            ))}
          </section>
        )}
      </div>
    </main>
  );
}

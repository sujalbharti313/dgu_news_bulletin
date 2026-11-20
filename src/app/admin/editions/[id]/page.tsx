import { EditionReview } from "@/components/admin/EditionReview";

export default async function EditionReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EditionReview id={id} />;
}

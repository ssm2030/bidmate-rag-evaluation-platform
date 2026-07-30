import { ReviewShell } from "@/components/eval-review/ReviewShell";

export default async function DatasetReviewPage({
  params,
}: {
  params: Promise<{ datasetId: string }>;
}) {
  const { datasetId } = await params;
  return <ReviewShell initialDatasetId={datasetId} />;
}

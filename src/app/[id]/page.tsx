import { notFound } from "next/navigation";

import { datasetRequestExists } from "@/lib/postgres";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export default async function RequestPage({
  params,
}: PageProps<"/[id]">) {
  const { id } = await params;

  if (!UUID_PATTERN.test(id) || !(await datasetRequestExists(id))) {
    notFound();
  }

  return (
    <main
      aria-label="Dataset request workspace"
      className="min-h-screen bg-white"
      data-request-id={id}
    />
  );
}

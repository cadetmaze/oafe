import Image from "next/image";
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
    <div className="grid h-dvh grid-rows-[64px_minmax(0,1fr)] overflow-hidden bg-white">
      <header className="flex h-16 items-center bg-white px-4">
        <Image
          alt="Ola Amigo"
          className="h-8 w-auto"
          height={32}
          priority
          src="/logo.png"
          width={142}
        />
        <h1 className="sr-only">Dataset Workspace</h1>
      </header>
      <main
        aria-label="Dataset request workspace"
        className="grid min-h-0 grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-4 px-4 pb-4"
        data-request-id={id}
      >
        <section
          aria-labelledby="prompt-chat-heading"
          className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-lg border border-[#E1E1E1] bg-white"
        >
          <h2 className="sr-only" id="prompt-chat-heading">Prompt and Chat</h2>
          <div
            aria-label="Chat window"
            className="min-h-0 flex-1 overflow-y-auto overscroll-contain"
          />
          <div
            aria-label="Prompting area"
            className="h-44 shrink-0 border-t border-[#E1E1E1] bg-[#F7F7F7]"
          />
        </section>
        <section
          aria-labelledby="preview-actions-heading"
          className="min-h-0 min-w-0 overflow-hidden rounded-lg border border-[#E1E1E1] bg-[#FAFAFA]"
        >
          <h2 className="sr-only" id="preview-actions-heading">Preview and Actions</h2>
          <div
            aria-label="Preview and action space"
            className="h-full overflow-y-auto overscroll-contain"
          />
        </section>
      </main>
    </div>
  );
}

import { ArrowLeft, DownloadSimple, UserPlus } from "@phosphor-icons/react/ssr";
import Image from "next/image";
import Link from "next/link";
import { notFound } from "next/navigation";

import WorkspaceChat from "@/components/workspace-chat";
import { getDatasetRequestSummary } from "@/lib/postgres";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export default async function RequestPage({
  params,
}: PageProps<"/[id]">) {
  const { id } = await params;
  const datasetRequest = UUID_PATTERN.test(id) ? await getDatasetRequestSummary(id) : null;

  if (!datasetRequest) {
    notFound();
  }

  return (
    <div className="grid h-dvh grid-rows-[64px_minmax(0,1fr)] overflow-hidden bg-white">
      <header className="flex h-16 items-center bg-white px-4">
        <Link
          aria-label="Back to dataset builder"
          className="group relative size-8 overflow-hidden rounded-lg border border-transparent text-[#423800] outline-none transition-[background-color,border-color] hover:border-[#E1E1E1] hover:bg-[#F7F7F7] focus-visible:border-[#E1E1E1] focus-visible:bg-[#F7F7F7]"
          href="/"
        >
          <Image
            alt=""
            className="h-8 w-auto max-w-none transition-[opacity,transform] duration-150 group-hover:scale-75 group-hover:opacity-0 group-focus-visible:scale-75 group-focus-visible:opacity-0"
            height={32}
            priority
            src="/logo.png"
            width={142}
          />
          <span className="absolute inset-0 flex items-center justify-center opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-visible:opacity-100">
            <ArrowLeft aria-hidden="true" size={16} weight="bold" />
          </span>
        </Link>
        <input
          aria-label="Dataset name"
          className="ml-2 h-8 w-56 max-w-[30vw] rounded-md border-0 bg-transparent px-2 font-geist text-[14px] font-medium text-[#282828] outline-none transition-colors hover:bg-[#F7F7F7] focus:bg-[#F7F7F7]"
          defaultValue="Untitled Dataset"
          maxLength={80}
          spellCheck={false}
          type="text"
        />
        <div className="ml-auto flex items-center gap-2">
          <button
            className="flex h-8 items-center justify-center gap-2 rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 font-geist text-[12px] font-medium text-[#423800] transition-[border-width,background-color] hover:bg-[#F1F1F1] active:border-b"
            type="button"
          >
            <UserPlus aria-hidden="true" size={13} weight="bold" />
            Invite Team
          </button>
          <button
            className="flex h-8 items-center justify-center gap-2 rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] px-4 font-geist text-[12px] font-medium text-[#423800] transition-[border-width] active:border-b"
            type="button"
          >
            <DownloadSimple aria-hidden="true" size={13} weight="bold" />
            Export Dataset
          </button>
        </div>
        <h1 className="sr-only">Dataset Workspace</h1>
      </header>
      <main
        aria-label="Dataset request workspace"
        className="grid min-h-0 grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-4 px-4 pb-4"
        data-request-id={id}
      >
        <section
          aria-labelledby="prompt-chat-heading"
          className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
        >
          <h2 className="sr-only" id="prompt-chat-heading">Prompt and Chat</h2>
          <WorkspaceChat initialQuery={datasetRequest.query} requestId={datasetRequest.id} />
        </section>
        <section
          aria-labelledby="preview-actions-heading"
          className="min-h-0 min-w-0 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-[#FAFAFA]"
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

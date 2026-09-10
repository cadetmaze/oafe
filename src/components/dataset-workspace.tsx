"use client";

import { useRef, useState } from "react";

import DatasetReviewStack, {
  type DatasetReviewConfirmation,
} from "@/components/dataset-review-stack";
import WorkspaceChat from "@/components/workspace-chat";

type DatasetWorkspaceProps = {
  initialQuery: string;
  requestId: string;
};

type ExternalAssistantMessage = {
  id: string;
  text: string;
};

function listLabels(labels: string[]) {
  return labels.length > 0 ? labels.join(", ") : "None";
}

export default function DatasetWorkspace({
  initialQuery,
  requestId,
}: DatasetWorkspaceProps) {
  const [externalAssistantMessage, setExternalAssistantMessage] =
    useState<ExternalAssistantMessage | null>(null);
  const deliveredSelectionsRef = useRef(new Set<string>());
  const confirmationSequenceRef = useRef(0);

  function confirmSelections(confirmation: DatasetReviewConfirmation) {
    const signature = JSON.stringify({
      approved: confirmation.approved.map(({ id }) => id),
      rejected: confirmation.rejected.map(({ id }) => id),
    });

    if (deliveredSelectionsRef.current.has(signature)) {
      return;
    }

    deliveredSelectionsRef.current.add(signature);
    confirmationSequenceRef.current += 1;

    const approvedLabels = confirmation.approved.map(({ label }) => label);
    const rejectedLabels = confirmation.rejected.map(({ label }) => label);

    setExternalAssistantMessage({
      id: `${requestId}-review-confirmation-${confirmationSequenceRef.current}`,
      text: [
        "Review confirmed.",
        `Approved: ${listLabels(approvedLabels)}.`,
        `Rejected: ${listLabels(rejectedLabels)}.`,
        "I’ll use these selections to refine the next results.",
      ].join("\n\n"),
    });
  }

  return (
    <>
      <section
        aria-labelledby="prompt-chat-heading"
        className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
      >
        <h2 className="sr-only" id="prompt-chat-heading">
          Prompt and Chat
        </h2>
        <WorkspaceChat
          externalAssistantMessage={externalAssistantMessage}
          initialQuery={initialQuery}
          requestId={requestId}
        />
      </section>
      <section
        aria-labelledby="preview-actions-heading"
        className="min-h-0 min-w-0 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-[#FAFAFA]"
      >
        <h2 className="sr-only" id="preview-actions-heading">
          Preview and Actions
        </h2>
        <div aria-label="Preview and action space" className="h-full min-h-0">
          <DatasetReviewStack onConfirm={confirmSelections} />
        </div>
      </section>
    </>
  );
}

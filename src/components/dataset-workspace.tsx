"use client";

import { useEffect, useRef, useState } from "react";

import DatasetReviewStack, {
  type DatasetReviewConfirmation,
  type DatasetReviewItem,
} from "@/components/dataset-review-stack";
import DatasetResultsGrid from "@/components/dataset-results-grid";
import WorkspaceChat from "@/components/workspace-chat";

type DatasetWorkspaceProps = {
  initialQuery: string;
  requestId: string;
};

type ExternalAssistantMessage = {
  id: string;
  markers: Array<{
    kind: "fetching" | "labeling";
    text: string;
  }>;
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
  const [confirmedReview, setConfirmedReview] =
    useState<DatasetReviewConfirmation | null>(null);
  const deliveredSelectionsRef = useRef(new Set<string>());
  const confirmationSequenceRef = useRef(0);
  const showMoreSequenceRef = useRef(0);

  useEffect(() => {
    const documentElement = document.documentElement;
    const body = document.body;
    const previousDocumentOverflow = documentElement.style.overflow;
    const previousDocumentOverscroll = documentElement.style.overscrollBehavior;
    const previousBodyOverflow = body.style.overflow;
    const previousBodyOverscroll = body.style.overscrollBehavior;

    documentElement.style.overflow = "hidden";
    documentElement.style.overscrollBehavior = "none";
    body.style.overflow = "hidden";
    body.style.overscrollBehavior = "none";

    return () => {
      documentElement.style.overflow = previousDocumentOverflow;
      documentElement.style.overscrollBehavior = previousDocumentOverscroll;
      body.style.overflow = previousBodyOverflow;
      body.style.overscrollBehavior = previousBodyOverscroll;
    };
  }, []);

  function showMoreExamples(nextBatchItems: readonly DatasetReviewItem[]) {
    showMoreSequenceRef.current += 1;
    setExternalAssistantMessage({
      id: `${requestId}-review-more-${showMoreSequenceRef.current}`,
      markers: [
        {
          kind: "fetching",
          text: `Fetched ${nextBatchItems.length} more candidate examples`,
        },
      ],
      text: `Here are ${nextBatchItems.length} more examples. Approve or reject each one and I’ll keep refining the results.`,
    });
  }

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
    setConfirmedReview(confirmation);

    const approvedLabels = confirmation.approved.map(({ label }) => label);
    const rejectedLabels = confirmation.rejected.map(({ label }) => label);

    setExternalAssistantMessage({
      id: `${requestId}-review-confirmation-${confirmationSequenceRef.current}`,
      markers: [
        {
          kind: "labeling",
          text: `Labeled ${confirmation.approved.length + confirmation.rejected.length} reviewed examples`,
        },
        {
          kind: "fetching",
          text: "Fetching refined dataset results",
        },
      ],
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
          {confirmedReview ? (
            <DatasetResultsGrid
              reviewedItems={[
                ...confirmedReview.approved,
                ...confirmedReview.rejected,
              ]}
            />
          ) : (
            <DatasetReviewStack
              onConfirm={confirmSelections}
              onShowMore={showMoreExamples}
            />
          )}
        </div>
      </section>
    </>
  );
}

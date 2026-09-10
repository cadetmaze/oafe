"use client";

import { Check, X } from "@phosphor-icons/react";
import Image from "next/image";
import { motion, useReducedMotion } from "motion/react";
import { useCallback, useRef, useState } from "react";

import GridReveal from "@/components/ui/grid-reveal";
import { cn } from "@/lib/utils";

export type DatasetReviewDecision = "approve" | "reject";

export type DatasetReviewItem = {
  id: string;
  src: string;
  alt: string;
  label: string;
};

export type DatasetReviewSummary = {
  approved: number;
  rejected: number;
};

export type DatasetReviewConfirmation = {
  approved: DatasetReviewItem[];
  rejected: DatasetReviewItem[];
};

export type DatasetReviewStackProps = {
  className?: string;
  items?: readonly DatasetReviewItem[];
  onConfirm?: (confirmation: DatasetReviewConfirmation) => void;
  onComplete?: (summary: DatasetReviewSummary) => void;
  onDecision?: (
    item: DatasetReviewItem,
    decision: DatasetReviewDecision,
  ) => void;
  onShowMore?: (nextBatchItems: readonly DatasetReviewItem[]) => void;
};

export const DEFAULT_REVIEW_ITEMS = [
  {
    id: "duolingo-learning",
    src: "/refero/duolingo.jpg",
    alt: "Duolingo learning interface",
    label: "Learning Experience",
  },
  {
    id: "endless-new-board",
    src: "/refero/endless-new-board.jpg",
    alt: "Endless board creation interface",
    label: "New Board Flow",
  },
  {
    id: "endless-video-map",
    src: "/refero/endless-video-map.jpg",
    alt: "Endless video map interface",
    label: "Video Map",
  },
  {
    id: "lovable-builder",
    src: "/refero/lovable.jpg",
    alt: "Lovable app builder interface",
    label: "App Builder",
  },
  {
    id: "mapbox-map",
    src: "/refero/mapbox.jpg",
    alt: "Mapbox mapping interface",
    label: "Interactive Map",
  },
  {
    id: "n8n-agent-workflow",
    src: "/refero/n8n-agent-workflow.jpg",
    alt: "N8N agent workflow interface",
    label: "Agent Workflow",
  },
  {
    id: "on-product-search",
    src: "/refero/on-product-search.jpg",
    alt: "On product search interface",
    label: "Product Search",
  },
  {
    id: "portrait-profile-editor",
    src: "/refero/portrait-profile-editor.jpg",
    alt: "Portrait profile editor interface",
    label: "Profile Editor",
  },
  {
    id: "pryzm-flow-editor",
    src: "/refero/pryzm-flow-editor.jpg",
    alt: "Pryzm flow editor interface",
    label: "Flow Editor",
  },
  {
    id: "runey-project",
    src: "/refero/runey-project.jpg",
    alt: "Runey project workspace interface",
    label: "Project Workspace",
  },
  {
    id: "airbnb-travel",
    src: "/refero/airbnb.jpg",
    alt: "Airbnb travel discovery interface",
    label: "Travel Discovery",
  },
  {
    id: "cursor-editor",
    src: "/refero/cursor.jpg",
    alt: "Cursor code editor interface",
    label: "Code Editor",
  },
  {
    id: "doctronic-health",
    src: "/refero/doctronic.jpg",
    alt: "Doctronic digital health interface",
    label: "Digital Health",
  },
  {
    id: "elevenreader-library",
    src: "/refero/elevenreader.jpg",
    alt: "ElevenReader audio library interface",
    label: "Audio Library",
  },
  {
    id: "endless-settings",
    src: "/refero/endless-settings.jpg",
    alt: "Endless workspace settings interface",
    label: "Workspace Settings",
  },
  {
    id: "fourmula-dashboard",
    src: "/refero/fourmula.jpg",
    alt: "Fourmula analytics dashboard interface",
    label: "Analytics Dashboard",
  },
  {
    id: "manus-assistant",
    src: "/refero/manus.jpg",
    alt: "Manus assistant workspace interface",
    label: "Assistant Workspace",
  },
  {
    id: "n8n-code-step",
    src: "/refero/n8n-code-step.jpg",
    alt: "N8N workflow code step interface",
    label: "Code Step",
  },
  {
    id: "n8n-trigger-browser",
    src: "/refero/n8n-trigger-browser.jpg",
    alt: "N8N trigger browser interface",
    label: "Trigger Browser",
  },
  {
    id: "on-order-tracking",
    src: "/refero/on-order-tracking.jpg",
    alt: "On order tracking interface",
    label: "Order Tracking",
  },
  {
    id: "programa-projects",
    src: "/refero/programa-projects.jpg",
    alt: "Programa projects interface",
    label: "Project Overview",
  },
  {
    id: "revolut-vault",
    src: "/refero/revolut-vault.jpg",
    alt: "Revolut vault interface",
    label: "Savings Vault",
  },
  {
    id: "acuity-scheduling",
    src: "/refero/acuity.jpg",
    alt: "Acuity scheduling interface",
    label: "Appointment Scheduling",
  },
  {
    id: "claude-assistant",
    src: "/refero/claude.jpg",
    alt: "Claude assistant interface",
    label: "AI Assistant",
  },
  {
    id: "cofounder-planning",
    src: "/refero/cofounder.jpg",
    alt: "Cofounder planning interface",
    label: "Product Planning",
  },
  {
    id: "dropbox-storage",
    src: "/refero/dropbox.jpg",
    alt: "Dropbox file storage interface",
    label: "File Storage",
  },
  {
    id: "monday-workspace",
    src: "/refero/monday.jpg",
    alt: "Monday work management interface",
    label: "Work Management",
  },
  {
    id: "on-sign-in",
    src: "/refero/on-sign-in.jpg",
    alt: "On sign-in interface",
    label: "Account Sign In",
  },
  {
    id: "portrait-hosting",
    src: "/refero/portrait-hosting.jpg",
    alt: "Portrait hosting interface",
    label: "Hosting Setup",
  },
  {
    id: "portrait-reserving",
    src: "/refero/portrait-reserving.jpg",
    alt: "Portrait reservation interface",
    label: "Reservation Flow",
  },
  {
    id: "programa-product-schedule",
    src: "/refero/programa-product-schedule.jpg",
    alt: "Programa product schedule interface",
    label: "Product Schedule",
  },
  {
    id: "programa-time-entry",
    src: "/refero/programa-time-entry.jpg",
    alt: "Programa time entry interface",
    label: "Time Entry",
  },
  {
    id: "pryzm-export",
    src: "/refero/pryzm-export.jpg",
    alt: "Pryzm export interface",
    label: "Export Flow",
  },
  {
    id: "pryzm-looks-gallery",
    src: "/refero/pryzm-looks-gallery.jpg",
    alt: "Pryzm looks gallery interface",
    label: "Looks Gallery",
  },
  {
    id: "revolut-sign-in",
    src: "/refero/revolut-sign-in.jpg",
    alt: "Revolut sign-in interface",
    label: "Secure Sign In",
  },
  {
    id: "revolut-spare-change",
    src: "/refero/revolut-spare-change.jpg",
    alt: "Revolut spare change interface",
    label: "Spare Change",
  },
  {
    id: "runey-client-invoices",
    src: "/refero/runey-client-invoices.jpg",
    alt: "Runey client invoices interface",
    label: "Client Invoices",
  },
  {
    id: "runey-dashboard",
    src: "/refero/runey-dashboard.jpg",
    alt: "Runey dashboard interface",
    label: "Business Dashboard",
  },
  {
    id: "spyglass-search",
    src: "/refero/spyglass.jpg",
    alt: "Spyglass search interface",
    label: "Visual Search",
  },
  {
    id: "synthesia-studio",
    src: "/refero/synthesia.jpg",
    alt: "Synthesia video studio interface",
    label: "Video Studio",
  },
] as const satisfies readonly DatasetReviewItem[];

const EMPTY_SUMMARY: DatasetReviewSummary = { approved: 0, rejected: 0 };
const MAX_REVIEW_ITEMS = 10;
const MAX_SHOW_MORE_COUNT = 3;

type ItemDecision = {
  decision: DatasetReviewDecision;
  item: DatasetReviewItem;
};

type PendingDecision = {
  batchStart: number;
  decision: DatasetReviewDecision;
  itemId: string;
  itemIndex: number;
};

export default function DatasetReviewStack({
  className,
  items = DEFAULT_REVIEW_ITEMS,
  onConfirm,
  onComplete,
  onDecision,
  onShowMore,
}: DatasetReviewStackProps) {
  const reduceMotion = useReducedMotion();
  const [batchStart, setBatchStart] = useState(0);
  const [showMoreCount, setShowMoreCount] = useState(0);
  const reviewItems = items.slice(
    batchStart,
    batchStart + MAX_REVIEW_ITEMS,
  );
  const [currentIndex, setCurrentIndex] = useState(0);
  const [pendingDecision, setPendingDecision] =
    useState<PendingDecision | null>(null);
  const [summary, setSummary] =
    useState<DatasetReviewSummary>(EMPTY_SUMMARY);
  const [itemDecisions, setItemDecisions] = useState<ItemDecision[]>([]);
  const [isConfirmed, setIsConfirmed] = useState(false);
  const confirmationSentRef = useRef(false);
  const completedDecisionKeysRef = useRef(new Set<string>());
  const [loadedAssetSources, setLoadedAssetSources] = useState<Set<string>>(
    () => new Set(),
  );

  const currentItem = reviewItems[currentIndex];
  const visibleItems = reviewItems.slice(currentIndex, currentIndex + 3);
  const isComplete = currentIndex >= reviewItems.length;
  const hasMoreItems =
    showMoreCount < MAX_SHOW_MORE_COUNT &&
    batchStart + reviewItems.length < items.length;
  const cumulativeSummary = itemDecisions.reduce<DatasetReviewSummary>(
    (totals, { decision }) => ({
      approved: totals.approved + (decision === "approve" ? 1 : 0),
      rejected: totals.rejected + (decision === "reject" ? 1 : 0),
    }),
    EMPTY_SUMMARY,
  );
  const isLoading = Boolean(
    currentItem && !loadedAssetSources.has(currentItem.src),
  );

  const markAssetLoaded = useCallback((assetSource: string) => {
    setLoadedAssetSources((loadedSources) => {
      if (loadedSources.has(assetSource)) {
        return loadedSources;
      }

      const nextLoadedSources = new Set(loadedSources);
      nextLoadedSources.add(assetSource);
      return nextLoadedSources;
    });
  }, []);

  function choose(decision: DatasetReviewDecision) {
    if (!currentItem || pendingDecision || isLoading) {
      return;
    }

    setPendingDecision({
      batchStart,
      decision,
      itemId: currentItem.id,
      itemIndex: currentIndex,
    });
  }

  function finishDecision() {
    if (!currentItem || !pendingDecision) {
      return;
    }

    if (
      pendingDecision.batchStart !== batchStart ||
      pendingDecision.itemIndex !== currentIndex ||
      pendingDecision.itemId !== currentItem.id
    ) {
      return;
    }

    const decisionKey = `${batchStart}:${currentIndex}:${currentItem.id}`;
    if (completedDecisionKeysRef.current.has(decisionKey)) {
      return;
    }

    completedDecisionKeysRef.current.add(decisionKey);
    const decision = pendingDecision.decision;
    const nextSummary = {
      approved: summary.approved + (decision === "approve" ? 1 : 0),
      rejected: summary.rejected + (decision === "reject" ? 1 : 0),
    };
    const nextIndex = currentIndex + 1;

    onDecision?.(currentItem, decision);
    setItemDecisions((decisions) => [
      ...decisions,
      { decision, item: currentItem },
    ]);
    setSummary(nextSummary);
    setCurrentIndex(nextIndex);
    setPendingDecision(null);

    if (nextIndex >= reviewItems.length) {
      onComplete?.(nextSummary);
    }
  }

  function showMore() {
    if (!hasMoreItems || isConfirmed || pendingDecision) {
      return;
    }

    const nextBatchStart = batchStart + reviewItems.length;
    const nextBatchItems = items.slice(
      nextBatchStart,
      nextBatchStart + MAX_REVIEW_ITEMS,
    );

    onShowMore?.(nextBatchItems);
    setBatchStart(nextBatchStart);
    setShowMoreCount((count) => count + 1);
    setCurrentIndex(0);
    setPendingDecision(null);
    setSummary(EMPTY_SUMMARY);
  }

  function confirmSelections() {
    if (confirmationSentRef.current) {
      return;
    }

    confirmationSentRef.current = true;
    setIsConfirmed(true);
    onConfirm?.({
      approved: itemDecisions
        .filter(({ decision }) => decision === "approve")
        .map(({ item }) => item),
      rejected: itemDecisions
        .filter(({ decision }) => decision === "reject")
        .map(({ item }) => item),
    });
  }

  return (
    <div
      className={cn(
        "flex h-full min-h-0 w-full items-center justify-center overflow-hidden p-5 font-geist sm:p-8",
        className,
      )}
    >
      {isComplete ? (
        <div className="flex w-full max-w-sm flex-col items-center rounded-[18px] border border-[#E1E1E1] bg-white px-6 py-8 text-center">
          <span className="mb-4 flex size-10 items-center justify-center rounded-full bg-[#EAF7EE] text-[#16803A]">
            <Check aria-hidden="true" size={20} weight="bold" />
          </span>
          <h3 className="text-[16px] font-medium text-[#282828]">
            Review Complete
          </h3>
          <p className="mt-1 text-[13px] text-[#777777]">
            {cumulativeSummary.approved} approved · {cumulativeSummary.rejected}{" "}
            rejected
          </p>
          {reviewItems.length > 0 ? (
            <div className="mt-5 flex flex-wrap items-center justify-center gap-2">
              {hasMoreItems ? (
                <button
                  className="h-8 cursor-pointer rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 text-[12px] font-medium text-[#423800] transition-colors hover:bg-[#F1F1F1] active:border-b disabled:cursor-default disabled:opacity-50"
                  disabled={isConfirmed}
                  onClick={showMore}
                  type="button"
                >
                  Show More
                </button>
              ) : null}
              <button
                className="h-8 cursor-pointer rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] px-4 text-[12px] font-medium text-[#423800] transition-colors hover:bg-[#F5CE00] active:border-b disabled:cursor-default disabled:opacity-50"
                disabled={isConfirmed}
                onClick={confirmSelections}
                type="button"
              >
                Confirm Selections
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="flex w-full max-w-[720px] flex-col items-center">
          <p className="mb-3 text-center text-[13px] font-normal text-[#423800]">
            Select correct examples to improve search accuracy.
          </p>
          <div className="relative aspect-video w-full">
            {visibleItems
              .slice(1)
              .map((item, itemIndex) => {
                const depth = itemIndex + 1;

                return (
                  <motion.div
                    aria-hidden="true"
                    animate={
                      pendingDecision
                        ? {
                            rotate: depth === 1 ? -0.45 : 0.6,
                            scale: depth === 1 ? 0.99 : 0.965,
                            y: depth === 1 ? 3 : 10,
                          }
                        : {
                            rotate: depth === 1 ? -1.25 : 1,
                            scale: depth === 1 ? 0.97 : 0.94,
                            y: depth === 1 ? 8 : 16,
                          }
                    }
                    className="absolute inset-0 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
                    initial={false}
                    key={item.id}
                    style={{ zIndex: 20 - depth }}
                    transition={{
                      duration: reduceMotion ? 0 : pendingDecision ? 0.34 : 0.28,
                      ease: [0.22, 1, 0.36, 1],
                    }}
                  >
                    <Image
                      alt=""
                      className="object-cover"
                      fill
                      onLoad={() => markAssetLoaded(item.src)}
                      sizes="(max-width: 900px) 70vw, 720px"
                      src={item.src}
                    />
                    <div className="absolute inset-0 bg-black/[0.04]" />
                  </motion.div>
                );
              })}

            <motion.article
              animate={
                pendingDecision
                  ? {
                      opacity: [1, 1, 0],
                      rotate:
                        pendingDecision.decision === "approve"
                          ? [0, 2.5, 12]
                          : [0, -2.5, -12],
                      scale: [1, 1.01, 0.98],
                      x:
                        pendingDecision.decision === "approve"
                          ? [0, 18, "125%"]
                          : [0, -18, "-125%"],
                      y: 0,
                    }
                  : { opacity: 1, rotate: 0, scale: 1, x: 0, y: 0 }
              }
              aria-label={`${currentItem.label}, item ${currentIndex + 1} of ${reviewItems.length}`}
              className="absolute inset-0 z-30 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
              initial={
                reduceMotion
                  ? false
                  : { opacity: 0, scale: 0.97, x: 0, y: 11 }
              }
              key={currentItem.id}
              onAnimationComplete={finishDecision}
              transition={{
                duration: reduceMotion ? 0 : pendingDecision ? 0.44 : 0.28,
                ease: [0.22, 1, 0.36, 1],
                times: pendingDecision ? [0, 0.16, 1] : undefined,
              }}
            >
              <Image
                alt={currentItem.alt}
                className="object-cover"
                fill
                onError={() => markAssetLoaded(currentItem.src)}
                onLoad={() => markAssetLoaded(currentItem.src)}
                priority={currentIndex < 2}
                sizes="(max-width: 900px) 70vw, 720px"
                src={currentItem.src}
              />

              {isLoading ? (
                <div className="pointer-events-none absolute inset-0">
                  <GridReveal
                    alt={currentItem.alt}
                    aspect={16 / 9}
                    className="h-full w-full rounded-[18px]"
                    estimatedDuration={500}
                    key={`reveal-${currentItem.src}`}
                    src={currentItem.src}
                  />
                </div>
              ) : (
                <>
                  <div className="pointer-events-none absolute inset-x-0 bottom-0 h-24 bg-gradient-to-t from-black/50 to-transparent" />
                  <p className="pointer-events-none absolute bottom-4 left-4 rounded-full bg-black/55 px-3 py-1.5 text-[12px] font-medium text-white backdrop-blur-sm">
                    {currentItem.label}
                  </p>
                </>
              )}

              {pendingDecision ? (
                <motion.div
                  animate={{ opacity: 1, scale: 1 }}
                  aria-hidden="true"
                  className="pointer-events-none absolute inset-0 z-20 flex items-center justify-center bg-black/10"
                  initial={{ opacity: 0, scale: 0.92 }}
                  transition={{ duration: reduceMotion ? 0 : 0.14 }}
                >
                  <span
                    className={cn(
                      "flex h-10 items-center gap-2 rounded-lg border px-3 text-[14px] font-medium text-white",
                      pendingDecision.decision === "approve"
                        ? "border-[#127035] bg-[#16803A]"
                        : "border-[#C93636] bg-[#E5484D]",
                    )}
                  >
                    {pendingDecision.decision === "approve" ? (
                      <Check aria-hidden="true" size={18} weight="bold" />
                    ) : (
                      <X aria-hidden="true" size={18} weight="bold" />
                    )}
                    {pendingDecision.decision === "approve"
                      ? "Approved"
                      : "Rejected"}
                  </span>
                </motion.div>
              ) : null}
            </motion.article>
          </div>

          <p aria-live="polite" className="sr-only" role="status">
            {isLoading
              ? `Loading item ${currentIndex + 1} of ${reviewItems.length}`
              : `Showing item ${currentIndex + 1} of ${reviewItems.length}`}
          </p>

          <div className="mt-8 grid w-full max-w-[420px] grid-cols-2 gap-3">
            <button
              className="flex h-11 cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#C93636] border-b-2 bg-[#E5484D] px-5 text-[14px] font-medium text-white transition-[border-width,background-color,opacity] hover:bg-[#D93D42] active:border-b disabled:cursor-default disabled:opacity-45"
              disabled={Boolean(pendingDecision) || isLoading}
              onClick={() => choose("reject")}
              type="button"
            >
              <X aria-hidden="true" size={18} weight="bold" />
              Reject
            </button>
            <button
              className="flex h-11 cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#127035] border-b-2 bg-[#16803A] px-5 text-[14px] font-medium text-white transition-[border-width,background-color,opacity] hover:bg-[#127035] active:border-b disabled:cursor-default disabled:opacity-45"
              disabled={Boolean(pendingDecision) || isLoading}
              onClick={() => choose("approve")}
              type="button"
            >
              <Check aria-hidden="true" size={18} weight="bold" />
              Approve
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

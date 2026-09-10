"use client";

import { Check, X } from "@phosphor-icons/react";
import Image from "next/image";
import { motion, useReducedMotion } from "motion/react";
import { useCallback, useState } from "react";

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

export type DatasetReviewStackProps = {
  className?: string;
  items?: readonly DatasetReviewItem[];
  onComplete?: (summary: DatasetReviewSummary) => void;
  onDecision?: (
    item: DatasetReviewItem,
    decision: DatasetReviewDecision,
  ) => void;
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
] as const satisfies readonly DatasetReviewItem[];

const EMPTY_SUMMARY: DatasetReviewSummary = { approved: 0, rejected: 0 };

export default function DatasetReviewStack({
  className,
  items = DEFAULT_REVIEW_ITEMS,
  onComplete,
  onDecision,
}: DatasetReviewStackProps) {
  const reduceMotion = useReducedMotion();
  const [currentIndex, setCurrentIndex] = useState(0);
  const [pendingDecision, setPendingDecision] =
    useState<DatasetReviewDecision | null>(null);
  const [summary, setSummary] =
    useState<DatasetReviewSummary>(EMPTY_SUMMARY);
  const [loadedAssetSources, setLoadedAssetSources] = useState<Set<string>>(
    () => new Set(),
  );

  const currentItem = items[currentIndex];
  const visibleItems = items.slice(currentIndex, currentIndex + 3);
  const isComplete = currentIndex >= items.length;
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

    setPendingDecision(decision);
  }

  function finishDecision() {
    if (!currentItem || !pendingDecision) {
      return;
    }

    const decision = pendingDecision;
    const nextSummary = {
      approved: summary.approved + (decision === "approve" ? 1 : 0),
      rejected: summary.rejected + (decision === "reject" ? 1 : 0),
    };
    const nextIndex = currentIndex + 1;

    onDecision?.(currentItem, decision);
    setSummary(nextSummary);
    setCurrentIndex(nextIndex);
    setPendingDecision(null);

    if (nextIndex >= items.length) {
      onComplete?.(nextSummary);
    }
  }

  function restart() {
    setCurrentIndex(0);
    setPendingDecision(null);
    setSummary(EMPTY_SUMMARY);
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
            {summary.approved} approved · {summary.rejected} rejected
          </p>
          {items.length > 0 ? (
            <button
              className="mt-5 h-8 cursor-pointer rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 text-[12px] font-medium text-[#423800] transition-colors hover:bg-[#F1F1F1] active:border-b"
              onClick={restart}
              type="button"
            >
              Review Again
            </button>
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
                    animate={{
                      rotate: depth === 1 ? -1.25 : 1,
                      scale: depth === 1 ? 0.97 : 0.94,
                      y: depth === 1 ? 8 : 16,
                    }}
                    className="absolute inset-0 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
                    initial={false}
                    key={item.id}
                    style={{ zIndex: 20 - depth }}
                    transition={{
                      duration: reduceMotion ? 0 : 0.28,
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
                      opacity: 0,
                      rotate: pendingDecision === "approve" ? 7 : -7,
                      x: pendingDecision === "approve" ? "118%" : "-118%",
                      y: 0,
                    }
                  : { opacity: 1, rotate: 0, scale: 1, x: 0, y: 0 }
              }
              aria-label={`${currentItem.label}, item ${currentIndex + 1} of ${items.length}`}
              className="absolute inset-0 z-30 overflow-hidden rounded-[18px] border border-[#E1E1E1] bg-white"
              initial={
                reduceMotion
                  ? false
                  : { opacity: 0, scale: 0.97, x: 0, y: 11 }
              }
              key={currentItem.id}
              onAnimationComplete={finishDecision}
              transition={{
                duration: reduceMotion ? 0 : pendingDecision ? 0.34 : 0.28,
                ease: [0.22, 1, 0.36, 1],
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
            </motion.article>
          </div>

          <p aria-live="polite" className="sr-only" role="status">
            {isLoading
              ? `Loading item ${currentIndex + 1} of ${items.length}`
              : `Showing item ${currentIndex + 1} of ${items.length}`}
          </p>

          <div className="mt-8 grid w-full max-w-[420px] grid-cols-2 gap-3">
            <button
              className="flex h-10 cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#C93636] border-b-2 bg-[#E5484D] px-5 text-[13px] font-medium text-white transition-[border-width,background-color,opacity] hover:bg-[#D93D42] active:border-b disabled:cursor-default disabled:opacity-45"
              disabled={Boolean(pendingDecision) || isLoading}
              onClick={() => choose("reject")}
              type="button"
            >
              <X aria-hidden="true" size={16} weight="bold" />
              Reject
            </button>
            <button
              className="flex h-10 cursor-pointer items-center justify-center gap-2 rounded-lg border border-[#127035] border-b-2 bg-[#16803A] px-5 text-[13px] font-medium text-white transition-[border-width,background-color,opacity] hover:bg-[#127035] active:border-b disabled:cursor-default disabled:opacity-45"
              disabled={Boolean(pendingDecision) || isLoading}
              onClick={() => choose("approve")}
              type="button"
            >
              <Check aria-hidden="true" size={16} weight="bold" />
              Approve
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

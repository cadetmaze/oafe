"use client";

import { useEffect, useMemo, useState } from "react";

import {
  DEFAULT_REVIEW_ITEMS,
  type DatasetReviewItem,
} from "@/components/dataset-review-stack";
import GridReveal from "@/components/ui/grid-reveal";

type DatasetResultsGridProps = {
  reviewedItems?: readonly DatasetReviewItem[];
};

type ResultEntry = {
  aspect: number;
  item: DatasetReviewItem;
};

const MAX_RESULTS = 24;
const REVEAL_INTERVAL_MS = 1_400;
const MASONRY_COLUMN_COUNT = 3;
const RESULT_ASPECTS = [4 / 5, 16 / 11, 1, 3 / 4, 4 / 3, 5 / 6] as const;

function arrangeInColumns(entries: readonly ResultEntry[]) {
  const columns = Array.from(
    { length: MASONRY_COLUMN_COUNT },
    () => [] as ResultEntry[],
  );
  const columnHeights = Array<number>(MASONRY_COLUMN_COUNT).fill(0);

  entries.forEach((entry) => {
    let shortestColumn = 0;

    for (let index = 1; index < MASONRY_COLUMN_COUNT; index += 1) {
      if (columnHeights[index] < columnHeights[shortestColumn]) {
        shortestColumn = index;
      }
    }

    columns[shortestColumn].push(entry);
    columnHeights[shortestColumn] += 1 / entry.aspect;
  });

  return columns;
}

export default function DatasetResultsGrid({
  reviewedItems = [],
}: DatasetResultsGridProps) {
  const resultEntries = useMemo(() => {
    const reviewedIds = new Set(reviewedItems.map(({ id }) => id));
    const unseenItems = DEFAULT_REVIEW_ITEMS.filter(
      ({ id }) => !reviewedIds.has(id),
    );
    const reviewedPoolItems = DEFAULT_REVIEW_ITEMS.filter(({ id }) =>
      reviewedIds.has(id),
    );

    return [...unseenItems, ...reviewedPoolItems]
      .slice(0, MAX_RESULTS)
      .map((item, index) => ({
        aspect: RESULT_ASPECTS[index % RESULT_ASPECTS.length],
        item,
      }));
  }, [reviewedItems]);
  const [visibleCount, setVisibleCount] = useState(() =>
    Math.min(1, resultEntries.length),
  );
  const shownCount = Math.min(visibleCount, resultEntries.length);
  const columns = arrangeInColumns(resultEntries.slice(0, shownCount));

  useEffect(() => {
    if (visibleCount >= resultEntries.length) {
      return;
    }

    const nextItemTimer = window.setTimeout(() => {
      setVisibleCount((count) => Math.min(count + 1, resultEntries.length));
    }, REVEAL_INTERVAL_MS);

    return () => window.clearTimeout(nextItemTimer);
  }, [resultEntries.length, visibleCount]);

  return (
    <div
      aria-busy={shownCount < resultEntries.length}
      aria-label="Found dataset examples"
      className="h-full min-h-0 overflow-y-auto p-2"
    >
      <div className="grid grid-cols-3 items-start gap-2">
        {columns.map((column, columnIndex) => (
          <div
            className="flex min-w-0 flex-col gap-2"
            key={`result-column-${columnIndex}`}
          >
            {column.map(({ aspect, item }) => (
              <GridReveal
                alt={item.alt}
                aspect={aspect}
                className="w-full rounded-lg border border-[#E1E1E1] bg-[#F4F4F5]"
                estimatedDuration={900}
                key={item.id}
                src={item.src}
              />
            ))}
          </div>
        ))}
      </div>

      <p aria-live="polite" className="sr-only" role="status">
        {shownCount < resultEntries.length
          ? `${shownCount} of ${resultEntries.length} dataset examples found`
          : `${resultEntries.length} dataset examples found`}
      </p>
    </div>
  );
}

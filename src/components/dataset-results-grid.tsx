"use client";

import { useEffect, useMemo, useState } from "react";

import {
  DEFAULT_REVIEW_ITEMS,
  type DatasetReviewItem,
} from "@/components/dataset-review-stack";
import GridReveal from "@/components/ui/grid-reveal";

export type DatasetResultRemoteContent = {
  thumbnail_uri?: string | null;
  preview_uri?: string | null;
  uri?: string | null;
  width?: number | null;
  height?: number | null;
};

export type DatasetResultRemoteAsset = {
  id: string;
  modality?: string | null;
  caption?: string | null;
  labels?: readonly string[] | null;
  tags?: readonly string[] | null;
  cached_uri?: string | null;
  content?: DatasetResultRemoteContent | null;
  src?: string | null;
  alt?: string | null;
  label?: string | null;
};

export type DatasetResultsGridProps = {
  emptyMessage?: string;
  error?: string | null;
  loading?: boolean;
  results?: readonly DatasetResultRemoteAsset[];
  reviewedItems?: readonly DatasetReviewItem[];
};

type NormalizedResultItem = DatasetReviewItem & {
  imageSources: string[];
  modality: string;
  videoSources: string[];
};

type ResultEntry = {
  aspect: number;
  item: NormalizedResultItem;
};

const MAX_MOCK_RESULTS = 24;
const REVEAL_INTERVAL_MS = 1_400;
const MASONRY_COLUMN_COUNT = 3;
const RESULT_ASPECTS = [4 / 5, 16 / 11, 1, 3 / 4, 4 / 3, 5 / 6] as const;

function uniqueSources(...sources: Array<string | null | undefined>) {
  return Array.from(
    new Set(
      sources
        .map((source) => source?.trim())
        .filter((source): source is string => Boolean(source)),
    ),
  );
}

function normalizedAspect(
  content: DatasetResultRemoteContent | null | undefined,
  fallback: number,
) {
  const width = content?.width;
  const height = content?.height;

  return typeof width === "number" &&
    Number.isFinite(width) &&
    width > 0 &&
    typeof height === "number" &&
    Number.isFinite(height) &&
    height > 0
    ? width / height
    : fallback;
}

function normalizeRemoteResult(
  asset: DatasetResultRemoteAsset,
  index: number,
): ResultEntry {
  const modality = asset.modality?.toLowerCase() || "image";
  const label =
    asset.label?.trim() ||
    asset.caption?.trim() ||
    asset.labels?.find(Boolean)?.trim() ||
    asset.tags?.find(Boolean)?.trim() ||
    "Dataset example";
  const alt = asset.alt?.trim() || asset.caption?.trim() || label;
  const isVideo = modality === "video";
  const isImage = modality === "image";
  const imageSources = uniqueSources(
    asset.src,
    asset.content?.thumbnail_uri,
    asset.content?.preview_uri,
    isImage ? asset.cached_uri : null,
    isImage ? asset.content?.uri : null,
  );

  return {
    aspect: normalizedAspect(
      asset.content,
      RESULT_ASPECTS[index % RESULT_ASPECTS.length],
    ),
    item: {
      id: asset.id,
      src: imageSources[0] ?? "",
      alt,
      label,
      imageSources,
      modality,
      videoSources: isVideo
        ? uniqueSources(asset.cached_uri, asset.content?.uri)
        : [],
    },
  };
}

function normalizeLocalResult(
  item: DatasetReviewItem,
  index: number,
): ResultEntry {
  return {
    aspect: RESULT_ASPECTS[index % RESULT_ASPECTS.length],
    item: {
      ...item,
      imageSources: uniqueSources(item.src),
      modality: "image",
      videoSources: [],
    },
  };
}

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

function ResultMedia({ aspect, item }: ResultEntry) {
  const [imageIndex, setImageIndex] = useState(0);
  const [videoIndex, setVideoIndex] = useState(0);
  const [imagesExhausted, setImagesExhausted] = useState(false);
  const [videosExhausted, setVideosExhausted] = useState(false);
  const imageSource = item.imageSources[imageIndex];
  const videoSource = item.videoSources[videoIndex];

  if (imageSource && !imagesExhausted) {
    return (
      <GridReveal
        alt={item.alt}
        aspect={aspect}
        className="w-full rounded-lg border border-[#E1E1E1] bg-[#F4F4F5]"
        estimatedDuration={900}
        onError={() => {
          if (imageIndex + 1 < item.imageSources.length) {
            setImageIndex((index) => index + 1);
          } else {
            setImagesExhausted(true);
          }
        }}
        src={imageSource}
      />
    );
  }

  if (item.modality === "video" && videoSource && !videosExhausted) {
    return (
      <div
        className="relative w-full overflow-hidden rounded-lg border border-[#E1E1E1] bg-[#F4F4F5]"
        style={{ aspectRatio: aspect }}
      >
        <video
          aria-label={item.alt}
          autoPlay
          className="absolute inset-0 size-full object-cover"
          loop
          muted
          onError={() => {
            if (videoIndex + 1 < item.videoSources.length) {
              setVideoIndex((index) => index + 1);
            } else {
              setVideosExhausted(true);
            }
          }}
          playsInline
          poster={item.imageSources[0]}
          preload="metadata"
          src={videoSource}
        />
      </div>
    );
  }

  return (
    <div
      aria-label={item.alt}
      className="flex w-full items-center justify-center rounded-lg border border-[#E1E1E1] bg-[#F4F4F5] px-3 text-center text-[11px] font-medium text-[#777777]"
      role="img"
      style={{ aspectRatio: aspect }}
    >
      {item.label}
    </div>
  );
}

function ProgressiveResults({
  emptyMessage,
  entries,
  error,
  loading,
}: {
  emptyMessage: string;
  entries: readonly ResultEntry[];
  error?: string | null;
  loading: boolean;
}) {
  const [visibleCount, setVisibleCount] = useState(() =>
    Math.min(1, entries.length),
  );
  const shownCount = Math.min(visibleCount, entries.length);
  const columns = arrangeInColumns(entries.slice(0, shownCount));

  useEffect(() => {
    if (visibleCount >= entries.length) {
      return;
    }

    const nextItemTimer = window.setTimeout(() => {
      setVisibleCount((count) => Math.min(count + 1, entries.length));
    }, REVEAL_INTERVAL_MS);

    return () => window.clearTimeout(nextItemTimer);
  }, [entries.length, visibleCount]);

  if (entries.length === 0) {
    return (
      <div
        aria-live="polite"
        className="flex h-full items-center justify-center px-6 text-center text-[13px] text-[#777777]"
        role={error ? "alert" : "status"}
      >
        {error ?? (loading ? "Building dataset results…" : emptyMessage)}
      </div>
    );
  }

  return (
    <div
      aria-busy={loading || shownCount < entries.length}
      aria-label="Found dataset examples"
      className="h-full min-h-0 overflow-y-auto overscroll-none p-2"
    >
      {error ? (
        <p
          className="mb-2 rounded-lg bg-[#FDECEC] px-3 py-2 text-[12px] text-[#A32D31]"
          role="alert"
        >
          {error}
        </p>
      ) : null}
      <div className="grid grid-cols-3 items-start gap-2">
        {columns.map((column, columnIndex) => (
          <div
            className="flex min-w-0 flex-col gap-2"
            key={`result-column-${columnIndex}`}
          >
            {column.map((entry) => (
              <ResultMedia {...entry} key={entry.item.id} />
            ))}
          </div>
        ))}
      </div>

      <p aria-live="polite" className="sr-only" role="status">
        {shownCount < entries.length
          ? `${shownCount} of ${entries.length} dataset examples found`
          : `${entries.length} dataset examples found`}
      </p>
    </div>
  );
}

export default function DatasetResultsGrid({
  emptyMessage = "No dataset results are available yet.",
  error,
  loading = false,
  results,
  reviewedItems = [],
}: DatasetResultsGridProps) {
  const resultEntries = useMemo(() => {
    if (results !== undefined) {
      return results.map(normalizeRemoteResult);
    }

    const reviewedIds = new Set(reviewedItems.map(({ id }) => id));
    const unseenItems = DEFAULT_REVIEW_ITEMS.filter(
      ({ id }) => !reviewedIds.has(id),
    );
    const reviewedPoolItems = DEFAULT_REVIEW_ITEMS.filter(({ id }) =>
      reviewedIds.has(id),
    );

    return [...unseenItems, ...reviewedPoolItems]
      .slice(0, MAX_MOCK_RESULTS)
      .map(normalizeLocalResult);
  }, [results, reviewedItems]);
  const collectionKey =
    results === undefined
      ? "mock-results"
      : `remote-results:${resultEntries[0]?.item.id ?? "empty"}`;

  return (
    <ProgressiveResults
      emptyMessage={emptyMessage}
      entries={resultEntries}
      error={error}
      key={collectionKey}
      loading={loading}
    />
  );
}

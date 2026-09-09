"use client";

import {
  ArrowUp,
  CaretDown,
  CaretRight,
  Check,
  Clock,
  CornersOut,
  Cube,
  FileText,
  Image as ImageIcon,
  Plus,
  SlidersHorizontal,
  Sparkle,
  SpeakerHigh,
  VideoCamera,
  X,
} from "@phosphor-icons/react";
import Image from "next/image";
import { useEffect, useMemo, useRef, useState } from "react";

import type { DatasetReference } from "@/types/dataset";
import type {
  DatasetContentType,
  DatasetDuration,
  DatasetMatchRange,
  DatasetOrientation,
  DatasetRequestDraft,
  DatasetRequestOptions,
} from "@/types/dataset-request";

export const DATASET_EXAMPLES = [
  "First-person video of hands folding laundry and stacking shirts by color.",
  "Overhead images of people organizing groceries into labeled kitchen drawers.",
  "Egocentric footage of a person assembling a wooden chair with hand tools.",
  "Short clips of workers sorting recyclable materials on a conveyor belt.",
  "Indoor robot-camera recordings of navigating around furniture and household objects.",
];

const MIN_EXAMPLES = 100;
const MAX_EXAMPLES = 10000;
const DEFAULT_EXAMPLES = 100;
const DEFAULT_SEED_MOODBOARDS = ["Product Inspiration", "Research References", "Landing Page Patterns"];

type FilterType = DatasetContentType;
type DurationFilter = DatasetDuration;
type OrientationFilter = DatasetOrientation;
type MatchRange = DatasetMatchRange;

const FILTER_OPTIONS = [
  { value: "image" as const, label: "Images", icon: ImageIcon, formats: ["png", "jpg", "webp", "gif"] },
  { value: "video" as const, label: "Video", icon: VideoCamera, formats: ["mp4", "mov", "webm"] },
  { value: "audio" as const, label: "Audio", icon: SpeakerHigh, formats: ["mp3", "wav", "m4a"] },
  { value: "document" as const, label: "Docs", icon: FileText, formats: ["pdf", "docx", "txt", "csv"] },
  { value: "3d" as const, label: "3D Files", icon: Cube, formats: ["glb", "gltf", "obj", "fbx"] },
];

const COMMON_FORMATS = ["png", "jpg", "mp4", "mov", "mp3", "pdf", "glb"];
const DURATION_OPTIONS: Array<{ value: DurationFilter; label: string }> = [
  { value: "any", label: "Any Length" },
  { value: "under-15", label: "Under 15s" },
  { value: "15-60", label: "15–60s" },
  { value: "1-5", label: "1–5 Min" },
  { value: "over-5", label: "Over 5 Min" },
];
const ORIENTATION_OPTIONS: Array<{ value: OrientationFilter; label: string }> = [
  { value: "any", label: "Any" },
  { value: "portrait", label: "Portrait" },
  { value: "landscape", label: "Landscape" },
  { value: "square", label: "Square" },
];
const MATCH_OPTIONS: Array<{ value: MatchRange; label: string }> = [
  { value: "very-high", label: "Very High" },
  { value: "high", label: "High" },
  { value: "balanced", label: "Balanced" },
  { value: "nearby", label: "Nearby" },
];

function clampExampleCount(value: number) {
  return Math.min(MAX_EXAMPLES, Math.max(MIN_EXAMPLES, Math.round(value)));
}

function getAvailableFormats(selectedTypes: FilterType[]) {
  if (selectedTypes.length === 0) {
    return COMMON_FORMATS;
  }

  return Array.from(
    new Set(
      selectedTypes.flatMap(
        (type) => FILTER_OPTIONS.find((option) => option.value === type)?.formats ?? [],
      ),
    ),
  );
}

function getEstimateMultiplier(selectedTypes: FilterType[], selectedFormats: string[]) {
  const typeWeights: Record<FilterType, number> = {
    image: 1,
    video: 1.65,
    audio: 1.15,
    document: 0.65,
    "3d": 2.25,
  };
  let activeTypes = selectedTypes;

  if (activeTypes.length === 0 && selectedFormats.length > 0) {
    activeTypes = FILTER_OPTIONS.filter((option) =>
      option.formats.some((format) => selectedFormats.includes(format)),
    ).map((option) => option.value);
  }

  if (activeTypes.length === 0) {
    return 1;
  }

  return activeTypes.reduce((total, type) => total + typeWeights[type], 0) / activeTypes.length;
}

function getTaskColor(progress: number) {
  const red = Math.round(229 + progress * 25);
  const green = Math.round(231 - progress * 16);
  const blue = Math.round(235 - progress * 235);
  return `rgb(${red} ${green} ${blue})`;
}

type Attachment = {
  file: File;
  id: string;
  url: string;
};

type PromptBoxProps = {
  isSubmitting?: boolean;
  moodboards?: string[];
  onDraftChange?: (draft: DatasetRequestOptions) => void;
  onQueryChange: (query: string) => void;
  onRemoveSeedDataReference?: (src: string) => void;
  onSelectSeedMoodboard?: (name: string) => void;
  onSubmit?: (draft: DatasetRequestDraft) => void;
  query: string;
  seedDataReferences?: DatasetReference[];
};

function FilePreview({ attachment, className }: { attachment: Attachment; className: string }) {
  const { file, url } = attachment;

  if (file.type.startsWith("image/")) {
    // Object URLs from local uploads cannot be optimized by next/image.
    // eslint-disable-next-line @next/next/no-img-element
    return <img alt={file.name} className={className} src={url} />;
  }

  if (file.type.startsWith("video/")) {
    return <video className={className} controls preload="metadata" src={url} />;
  }

  if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) {
    return <iframe className={className} src={url} title={file.name} />;
  }

  return (
    <div className={`${className} flex items-center justify-center bg-[#F7F7F7]`}>
      <FileText color="#423800" size={28} weight="regular" />
    </div>
  );
}

export default function PromptBox({
  isSubmitting = false,
  moodboards = DEFAULT_SEED_MOODBOARDS,
  onDraftChange,
  onQueryChange,
  onRemoveSeedDataReference,
  onSelectSeedMoodboard,
  onSubmit,
  query,
  seedDataReferences = [],
}: PromptBoxProps) {
  const [exampleIndex, setExampleIndex] = useState(0);
  const [placeholder, setPlaceholder] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const [isPromptFocused, setIsPromptFocused] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [activeAttachmentId, setActiveAttachmentId] = useState<string | null>(null);
  const [exampleCount, setExampleCount] = useState(DEFAULT_EXAMPLES);
  const [exampleCountInput, setExampleCountInput] = useState(String(DEFAULT_EXAMPLES));
  const [isExamplesOpen, setIsExamplesOpen] = useState(false);
  const [isFiltersOpen, setIsFiltersOpen] = useState(false);
  const [isSeedDataMenuOpen, setIsSeedDataMenuOpen] = useState(false);
  const [isSeedMoodboardMenuOpen, setIsSeedMoodboardMenuOpen] = useState(false);
  const [selectedSeedMoodboard, setSelectedSeedMoodboard] = useState<string | null>(null);
  const [selectedFilterTypes, setSelectedFilterTypes] = useState<FilterType[]>([]);
  const [selectedDuration, setSelectedDuration] = useState<DurationFilter>("any");
  const [selectedOrientation, setSelectedOrientation] = useState<OrientationFilter>("any");
  const [selectedFormats, setSelectedFormats] = useState<string[]>([]);
  const [selectedMatchRange, setSelectedMatchRange] = useState<MatchRange>("balanced");
  const [activeFilterSection, setActiveFilterSection] = useState<
    "content" | "duration" | "dimensions" | "match" | "format" | null
  >(null);
  const seedDataInputRef = useRef<HTMLInputElement>(null);
  const promptInputRef = useRef<HTMLTextAreaElement>(null);
  const attachmentsRef = useRef<Attachment[]>([]);
  const filtersPopoverRef = useRef<HTMLDivElement>(null);
  const examplesPopoverRef = useRef<HTMLDivElement>(null);
  const seedDataPopoverRef = useRef<HTMLDivElement>(null);
  const submenuCloseTimeoutRef = useRef<number | null>(null);
  const seedMoodboardCloseTimeoutRef = useRef<number | null>(null);

  useEffect(() => {
    attachmentsRef.current = attachments;
  }, [attachments]);

  useEffect(() => {
    return () => {
      attachmentsRef.current.forEach((attachment) => URL.revokeObjectURL(attachment.url));
    };
  }, []);

  const activeAttachment = attachments.find((attachment) => attachment.id === activeAttachmentId);
  const exampleProgress = (exampleCount - MIN_EXAMPLES) / (MAX_EXAMPLES - MIN_EXAMPLES);
  const taskColor = getTaskColor(exampleProgress);
  const availableFormats = getAvailableFormats(selectedFilterTypes);
  const estimateMultiplier = getEstimateMultiplier(selectedFilterTypes, selectedFormats);
  const estimatedMinMinutes = Math.max(3, Math.round((7 + exampleProgress * 35) * estimateMultiplier));
  const estimatedMaxMinutes = estimatedMinMinutes + Math.max(2, Math.round(3 * estimateMultiplier));
  const showVideoFilters = selectedFilterTypes.length === 0 || selectedFilterTypes.includes("video");
  const showVisualFilters =
    selectedFilterTypes.length === 0 ||
    selectedFilterTypes.includes("image") ||
    selectedFilterTypes.includes("video");
  const activeFilterCount =
    selectedFilterTypes.length +
    (selectedDuration !== "any" ? 1 : 0) +
    (selectedOrientation !== "any" ? 1 : 0) +
    selectedFormats.length +
    (selectedMatchRange !== "balanced" ? 1 : 0);
  const visibleFilterSections = [
    "content",
    ...(showVideoFilters ? ["duration"] : []),
    ...(showVisualFilters ? ["dimensions"] : []),
    "match",
    "format",
  ];
  const activeFilterIndex = activeFilterSection ? visibleFilterSections.indexOf(activeFilterSection) : 0;
  const submenuTopOffset = Math.max(0, activeFilterIndex) * 40;
  const requestOptions = useMemo<DatasetRequestOptions>(
    () => ({
      estimatedTimeMinutes: {
        max: estimatedMaxMinutes,
        min: estimatedMinMinutes,
      },
      exampleCount,
      filters: {
        contentTypes: selectedFilterTypes,
        duration: selectedDuration,
        formats: selectedFormats,
        matchRange: selectedMatchRange,
        orientation: selectedOrientation,
      },
      seedData: {
        references: seedDataReferences,
        selectedMoodboard: selectedSeedMoodboard,
        uploads: attachments.map((attachment) => attachment.file),
      },
    }),
    [
      attachments,
      estimatedMaxMinutes,
      estimatedMinMinutes,
      exampleCount,
      seedDataReferences,
      selectedDuration,
      selectedFilterTypes,
      selectedFormats,
      selectedMatchRange,
      selectedOrientation,
      selectedSeedMoodboard,
    ],
  );

  useEffect(() => {
    onDraftChange?.(requestOptions);
  }, [onDraftChange, requestOptions]);

  useEffect(() => {
    if (!isFiltersOpen && !isExamplesOpen && !isSeedDataMenuOpen) {
      return;
    }

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      const element = event.target as Element;
      const filtersTrigger = element.closest("[data-filters-trigger]");

      if (isFiltersOpen && !filtersPopoverRef.current?.contains(target) && !filtersTrigger) {
        setIsFiltersOpen(false);
        setActiveFilterSection(null);
      }
      if (isExamplesOpen && !examplesPopoverRef.current?.contains(target)) {
        setIsExamplesOpen(false);
      }
      if (isSeedDataMenuOpen && !seedDataPopoverRef.current?.contains(target)) {
        setIsSeedDataMenuOpen(false);
        setIsSeedMoodboardMenuOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsFiltersOpen(false);
        setActiveFilterSection(null);
        setIsExamplesOpen(false);
        setIsSeedDataMenuOpen(false);
        setIsSeedMoodboardMenuOpen(false);
      }
    };

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isExamplesOpen, isFiltersOpen, isSeedDataMenuOpen]);

  useEffect(() => {
    return () => {
      if (submenuCloseTimeoutRef.current !== null) {
        window.clearTimeout(submenuCloseTimeoutRef.current);
      }
      if (seedMoodboardCloseTimeoutRef.current !== null) {
        window.clearTimeout(seedMoodboardCloseTimeoutRef.current);
      }
    };
  }, []);

  const removeAttachment = (id: string) => {
    const attachment = attachmentsRef.current.find((item) => item.id === id);

    if (attachment) {
      URL.revokeObjectURL(attachment.url);
    }

    setAttachments((currentAttachments) => currentAttachments.filter((item) => item.id !== id));
    setActiveAttachmentId((currentId) => (currentId === id ? null : currentId));
  };

  const toggleFilterType = (type: FilterType) => {
    setSelectedFilterTypes((currentTypes) => {
      const nextTypes = currentTypes.includes(type)
        ? currentTypes.filter((currentType) => currentType !== type)
        : [...currentTypes, type];
      const nextFormats = getAvailableFormats(nextTypes);

      setSelectedFormats((currentFormats) => currentFormats.filter((format) => nextFormats.includes(format)));
      return nextTypes;
    });
  };

  const clearFilters = () => {
    setSelectedFilterTypes([]);
    setSelectedDuration("any");
    setSelectedOrientation("any");
    setSelectedFormats([]);
    setSelectedMatchRange("balanced");
  };

  const cancelSubmenuClose = () => {
    if (submenuCloseTimeoutRef.current !== null) {
      window.clearTimeout(submenuCloseTimeoutRef.current);
      submenuCloseTimeoutRef.current = null;
    }
  };

  const scheduleSubmenuClose = () => {
    cancelSubmenuClose();
    submenuCloseTimeoutRef.current = window.setTimeout(() => {
      setActiveFilterSection(null);
      submenuCloseTimeoutRef.current = null;
    }, 1000);
  };

  const cancelSeedMoodboardClose = () => {
    if (seedMoodboardCloseTimeoutRef.current !== null) {
      window.clearTimeout(seedMoodboardCloseTimeoutRef.current);
      seedMoodboardCloseTimeoutRef.current = null;
    }
  };

  const scheduleSeedMoodboardClose = () => {
    cancelSeedMoodboardClose();
    seedMoodboardCloseTimeoutRef.current = window.setTimeout(() => {
      setIsSeedMoodboardMenuOpen(false);
      seedMoodboardCloseTimeoutRef.current = null;
    }, 1000);
  };

  useEffect(() => {
    const example = DATASET_EXAMPLES[exampleIndex];
    const isFullyTyped = placeholder === example;
    const isFullyDeleted = placeholder === "";
    const delay = isDeleting ? (isFullyDeleted ? 500 : 24) : isFullyTyped ? 5000 : 38;

    const timer = window.setTimeout(() => {
      if (isDeleting) {
        if (isFullyDeleted) {
          setIsDeleting(false);
          setExampleIndex((currentIndex) => (currentIndex + 1) % DATASET_EXAMPLES.length);
        } else {
          setPlaceholder(placeholder.slice(0, -1));
        }
      } else if (isFullyTyped) {
        setIsDeleting(true);
      } else {
        setPlaceholder(example.slice(0, placeholder.length + 1));
      }
    }, delay);

    return () => window.clearTimeout(timer);
  }, [exampleIndex, isDeleting, placeholder]);

  const submitRequest = () => {
    const normalizedQuery = query.trim();

    if (!normalizedQuery || isSubmitting) {
      promptInputRef.current?.focus();
      return;
    }

    const parsedExampleCount = Number(exampleCountInput);
    const normalizedExampleCount = Number.isFinite(parsedExampleCount)
      ? clampExampleCount(parsedExampleCount)
      : exampleCount;
    const normalizedProgress =
      (normalizedExampleCount - MIN_EXAMPLES) / (MAX_EXAMPLES - MIN_EXAMPLES);
    const normalizedMinMinutes = Math.max(
      3,
      Math.round((7 + normalizedProgress * 35) * estimateMultiplier),
    );
    const normalizedMaxMinutes =
      normalizedMinMinutes + Math.max(2, Math.round(3 * estimateMultiplier));

    if (normalizedExampleCount !== exampleCount) {
      setExampleCount(normalizedExampleCount);
      setExampleCountInput(String(normalizedExampleCount));
    }

    onSubmit?.({
      ...requestOptions,
      estimatedTimeMinutes: {
        max: normalizedMaxMinutes,
        min: normalizedMinMinutes,
      },
      exampleCount: normalizedExampleCount,
      query: normalizedQuery,
    });
  };

  return (
    <div
      className="box-border min-h-[200px] w-[850px] rounded-[32px] border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] p-[7px] pb-[6px]"
      onClick={(event) => {
        const target = event.target as Element;

        if (!target.closest("button, input, textarea, [role='dialog'], [role='menu']")) {
          promptInputRef.current?.focus();
        }
      }}
    >
      {(attachments.length > 0 || seedDataReferences.length > 0) && (
        <div className="mb-2 flex max-w-full gap-3 overflow-x-auto px-2 pt-1 pb-1">
          {seedDataReferences.map((reference) => (
            <div className="w-[120px] shrink-0" key={reference.src}>
              <div className="relative size-[100px] overflow-hidden rounded-lg border border-[#E1E1E1] bg-white">
                <Image
                  alt={reference.alt}
                  className="object-cover"
                  fill
                  sizes="100px"
                  src={reference.src}
                />
              </div>
              <div className="mt-2 flex w-[100px] items-center gap-1">
                <span className="min-w-0 flex-1 truncate font-geist text-[11px] font-medium leading-4 text-[#423800]">
                  {reference.alt}
                </span>
                <button
                  aria-label={`Remove ${reference.alt}`}
                  className="flex size-4 shrink-0 cursor-pointer items-center justify-center rounded text-[#423800]"
                  onClick={() => onRemoveSeedDataReference?.(reference.src)}
                  type="button"
                >
                  <X size={12} weight="bold" />
                </button>
              </div>
            </div>
          ))}
          {attachments.map((attachment) => (
            <div className="w-[120px] shrink-0" key={attachment.id}>
              <button
                aria-label={`Preview ${attachment.file.name}`}
                className="block size-[100px] cursor-pointer overflow-hidden rounded-lg border border-[#E1E1E1] bg-white"
                onClick={() => setActiveAttachmentId(attachment.id)}
                type="button"
              >
                <FilePreview attachment={attachment} className="size-full object-cover" />
              </button>
              <div className="mt-2 flex w-[100px] items-center gap-1">
                <span className="min-w-0 flex-1 truncate font-geist text-[11px] font-medium leading-4 text-[#423800]">
                  {attachment.file.name}
                </span>
                <button
                  aria-label={`Remove ${attachment.file.name}`}
                  className="flex size-4 shrink-0 cursor-pointer items-center justify-center rounded text-[#423800]"
                  onClick={() => removeAttachment(attachment.id)}
                  type="button"
                >
                  <X size={12} weight="bold" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
      <input
        ref={seedDataInputRef}
        aria-label="Choose seed data files"
        accept="image/*,video/*,audio/*,application/pdf,.doc,.docx,.txt,.csv,.glb,.gltf,.obj,.fbx"
        className="absolute h-px w-px overflow-hidden opacity-0"
        multiple
        onChange={(event) => {
          const selectedFiles = Array.from(event.target.files ?? []);
          const newAttachments = selectedFiles.map((file) => ({
            file,
            id: `${file.name}-${file.lastModified}-${Math.random()}`,
            url: URL.createObjectURL(file),
          }));

          setAttachments((currentAttachments) => [...currentAttachments, ...newAttachments]);
          event.currentTarget.value = "";
        }}
        type="file"
      />
      <div className="relative box-border h-[184px] w-full rounded-[24px] bg-white">
        <svg
          aria-hidden="true"
          className="pointer-events-none absolute -inset-px h-[calc(100%+2px)] w-[calc(100%+2px)]"
          preserveAspectRatio="none"
          viewBox="0 0 836 186"
        >
          <rect
            fill="white"
            height="185"
            rx="24"
            stroke="#EEEEEE"
            strokeDasharray="8 8"
            strokeWidth="1"
            width="835"
            x="0.5"
            y="0.5"
          />
        </svg>
        {query.length === 0 && !isPromptFocused && (
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-4 top-4 h-12 overflow-hidden whitespace-nowrap font-geist text-[16px] font-light leading-6 text-[#A3A3A3]"
          >
            {placeholder}
          </div>
        )}
        <textarea
          aria-label="Prompt"
          className="absolute inset-x-4 top-4 h-24 max-h-24 resize-none overflow-y-auto border-0 bg-transparent p-0 font-geist text-[16px] font-light leading-6 text-[#282828] placeholder:text-[#A3A3A3] focus:outline-none"
          onChange={(event) => onQueryChange(event.target.value)}
          onBlur={() => setIsPromptFocused(false)}
          onFocus={() => setIsPromptFocused(true)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              submitRequest();
            }
          }}
          ref={promptInputRef}
          value={query}
        />
        <div className="absolute bottom-4 left-4 flex items-end gap-2">
          <div className="relative" ref={seedDataPopoverRef}>
            {isSeedDataMenuOpen && (
              <div
                aria-label="Seed data options"
                className="absolute left-0 top-[calc(100%+4px)] z-50 w-[232px] rounded-xl border border-[#E1E1E1] bg-white p-1.5 text-[#423800] shadow-[0_12px_28px_rgba(66,56,0,0.08)]"
                role="menu"
              >
                <button
                  className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[#F7F7F7] focus-visible:bg-[#F7F7F7] focus-visible:outline-none"
                  onClick={() => {
                    cancelSeedMoodboardClose();
                    setIsSeedDataMenuOpen(false);
                    setIsSeedMoodboardMenuOpen(false);
                    seedDataInputRef.current?.click();
                  }}
                  role="menuitem"
                  type="button"
                >
                  <ArrowUp color="#423800" size={15} weight="regular" />
                  <span className="flex-1 font-geist text-[12px] font-medium">Upload Files</span>
                </button>
                <div
                  className="relative"
                  onMouseEnter={() => {
                    cancelSeedMoodboardClose();
                    setIsSeedMoodboardMenuOpen(true);
                  }}
                  onMouseLeave={scheduleSeedMoodboardClose}
                >
                  <button
                    aria-expanded={isSeedMoodboardMenuOpen}
                    aria-haspopup="menu"
                    className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors hover:bg-[#F7F7F7] focus-visible:bg-[#F7F7F7] focus-visible:outline-none"
                    onClick={() => {
                      cancelSeedMoodboardClose();
                      setIsSeedMoodboardMenuOpen(true);
                    }}
                    role="menuitem"
                    type="button"
                  >
                    <ImageIcon color="#423800" size={15} weight="regular" />
                    <span className="min-w-0 flex-1 truncate font-geist text-[12px] font-medium">Choose From Moodboard</span>
                    <CaretRight color="#999999" size={13} weight="bold" />
                  </button>
                  {isSeedMoodboardMenuOpen && (
                    <div
                      aria-label="Choose from moodboard"
                      className="absolute left-[calc(100%+2px)] top-0 z-50 w-[190px] rounded-xl border border-[#E1E1E1] bg-white p-1.5 text-[#423800] shadow-[0_12px_28px_rgba(66,56,0,0.08)]"
                      onMouseEnter={cancelSeedMoodboardClose}
                      onMouseLeave={scheduleSeedMoodboardClose}
                      role="menu"
                    >
                      {moodboards.map((moodboard) => {
                        const isSelected = selectedSeedMoodboard === moodboard;

                        return (
                          <button
                            aria-checked={isSelected}
                            className={`flex w-full cursor-pointer items-center gap-2 rounded-lg px-2.5 py-2 text-left transition-colors focus-visible:outline-none ${
                              isSelected ? "bg-[#F4F4F5] text-[#09090B]" : "hover:bg-[#F7F7F7] focus-visible:bg-[#F7F7F7]"
                            }`}
                            key={moodboard}
                            onClick={() => {
                              cancelSeedMoodboardClose();
                              setSelectedSeedMoodboard(moodboard);
                              onSelectSeedMoodboard?.(moodboard);
                              setIsSeedMoodboardMenuOpen(false);
                              setIsSeedDataMenuOpen(false);
                            }}
                            role="menuitemradio"
                            type="button"
                          >
                            <span className="min-w-0 flex-1 truncate font-geist text-[12px] font-medium">{moodboard}</span>
                            <span
                              aria-hidden="true"
                              className={`size-2 shrink-0 rounded-full ${isSelected ? "bg-[#FED700] ring-1 ring-[#EDC800]" : "bg-transparent"}`}
                            />
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              </div>
            )}
            <button
              aria-expanded={isSeedDataMenuOpen}
              aria-haspopup="menu"
              aria-label="Seed data"
              className="flex h-8 cursor-pointer items-center justify-center gap-2 overflow-hidden rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 text-[#423800] transition-[border-width] duration-150 ease-out active:border-b"
              data-seed-data-trigger
              onClick={() => {
                cancelSeedMoodboardClose();
                setIsSeedDataMenuOpen((isOpen) => !isOpen);
                setIsSeedMoodboardMenuOpen(false);
                setIsFiltersOpen(false);
                setActiveFilterSection(null);
                setIsExamplesOpen(false);
              }}
              type="button"
            >
              <Plus
                color="#423800"
                size={12}
                weight="bold"
              />
              <span className="font-geist text-[12px] font-medium leading-none">
                Seed Data
              </span>
            </button>
          </div>
          <div className="relative" ref={filtersPopoverRef}>
            {isFiltersOpen && (
              <>
                <div
                  aria-label="Dataset filters"
                  className="absolute left-0 top-[calc(100%+4px)] z-40 w-[276px] rounded-[18px] border border-[#E1E1E1] bg-white p-2 text-[#423800] shadow-[0_12px_28px_rgba(66,56,0,0.08)]"
                  onMouseEnter={cancelSubmenuClose}
                  onMouseLeave={scheduleSubmenuClose}
                  role="menu"
                >
                  <div className="space-y-0.5">
                    <button
                      aria-expanded={activeFilterSection === "content"}
                      aria-haspopup="menu"
                      className="flex w-full cursor-pointer items-center gap-1.5 rounded-xl px-2.5 py-2.5 text-left transition-colors hover:bg-[#F7F7F7]"
                      onClick={() => setActiveFilterSection("content")}
                      onMouseEnter={() => setActiveFilterSection("content")}
                      role="menuitem"
                      type="button"
                    >
                      <FileText color="#423800" size={16} weight="regular" />
                      <span className="min-w-0 flex-1 truncate whitespace-nowrap font-geist text-[12px] font-medium">Content Type</span>
                      <span className="max-w-[130px] truncate font-geist text-[11px] font-light text-[#888888]">
                        {selectedFilterTypes.length === 0 ? "Any" : selectedFilterTypes.map((type) => FILTER_OPTIONS.find((option) => option.value === type)?.label).join(", ")}
                      </span>
                      {activeFilterSection === "content" ? <CaretRight color="#999999" size={14} /> : <CaretDown color="#999999" size={14} />}
                    </button>
                    {showVideoFilters && (
                    <button
                      aria-expanded={activeFilterSection === "duration"}
                      aria-haspopup="menu"
                      className="flex w-full cursor-pointer items-center gap-1.5 rounded-xl px-2.5 py-2.5 text-left transition-colors hover:bg-[#F7F7F7]"
                      onClick={() => setActiveFilterSection("duration")}
                      onMouseEnter={() => setActiveFilterSection("duration")}
                      role="menuitem"
                      type="button"
                    >
                        <Clock color="#423800" size={16} weight="regular" />
                        <span className="min-w-0 flex-1 truncate whitespace-nowrap font-geist text-[12px] font-medium">Clip Duration</span>
                        <span className="font-geist text-[11px] font-light text-[#888888]">{DURATION_OPTIONS.find((option) => option.value === selectedDuration)?.label}</span>
                        {activeFilterSection === "duration" ? <CaretRight color="#999999" size={14} /> : <CaretDown color="#999999" size={14} />}
                      </button>
                    )}
                    {showVisualFilters && (
                      <button
                        aria-expanded={activeFilterSection === "dimensions"}
                        aria-haspopup="menu"
                        className="flex w-full cursor-pointer items-center gap-1.5 rounded-xl px-2.5 py-2.5 text-left transition-colors hover:bg-[#F7F7F7]"
                        onClick={() => setActiveFilterSection("dimensions")}
                        onMouseEnter={() => setActiveFilterSection("dimensions")}
                        role="menuitem"
                        type="button"
                      >
                        <CornersOut color="#423800" size={16} weight="regular" />
                        <span className="min-w-0 flex-1 truncate whitespace-nowrap font-geist text-[12px] font-medium">Dimensions</span>
                        <span className="font-geist text-[11px] font-light text-[#888888]">
                          {ORIENTATION_OPTIONS.find((option) => option.value === selectedOrientation)?.label}
                        </span>
                        {activeFilterSection === "dimensions" ? <CaretRight color="#999999" size={14} /> : <CaretDown color="#999999" size={14} />}
                      </button>
                    )}
                    <button
                      aria-expanded={activeFilterSection === "match"}
                      aria-haspopup="menu"
                      className="flex w-full cursor-pointer items-center gap-1.5 rounded-xl px-2.5 py-2.5 text-left transition-colors hover:bg-[#F7F7F7]"
                      onClick={() => setActiveFilterSection("match")}
                      onMouseEnter={() => setActiveFilterSection("match")}
                      role="menuitem"
                      type="button"
                    >
                      <Sparkle color="#423800" size={16} weight="regular" />
                      <span className="min-w-0 flex-1 truncate whitespace-nowrap font-geist text-[12px] font-medium">Match With Seed Data</span>
                      <span className="font-geist text-[11px] font-light text-[#888888]">{MATCH_OPTIONS.find((option) => option.value === selectedMatchRange)?.label}</span>
                      {activeFilterSection === "match" ? <CaretRight color="#999999" size={14} /> : <CaretDown color="#999999" size={14} />}
                    </button>
                    <button
                      aria-expanded={activeFilterSection === "format"}
                      aria-haspopup="menu"
                      className="flex w-full cursor-pointer items-center gap-1.5 rounded-xl px-2.5 py-2.5 text-left transition-colors hover:bg-[#F7F7F7]"
                      onClick={() => setActiveFilterSection("format")}
                      onMouseEnter={() => setActiveFilterSection("format")}
                      role="menuitem"
                      type="button"
                    >
                      <FileText color="#423800" size={16} weight="regular" />
                      <span className="min-w-0 flex-1 truncate whitespace-nowrap font-geist text-[12px] font-medium">Format</span>
                      <span className="font-geist text-[11px] font-light text-[#888888]">{selectedFormats.length === 0 ? "Any" : `${selectedFormats.length} selected`}</span>
                      {activeFilterSection === "format" ? <CaretRight color="#999999" size={14} /> : <CaretDown color="#999999" size={14} />}
                    </button>
                  </div>
                  {activeFilterCount > 0 && (
                    <div className="mt-1 flex justify-end border-t border-[#EEEEEE] pt-2">
                      <button
                        className="flex h-7 cursor-pointer items-center justify-center rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F3F3F3] px-3 font-geist text-[11px] font-medium text-[#423800] transition-[border-width,background-color] duration-150 ease-out hover:bg-[#EAEAEA] active:border-b"
                        onClick={clearFilters}
                        type="button"
                      >
                        Clear Filters
                      </button>
                    </div>
                  )}
                </div>

                {activeFilterSection && (
                  <div
                    aria-label="Filter options"
                    className="absolute left-[278px] z-50 max-w-[200px] w-[200px] rounded-[18px] border border-[#E1E1E1] bg-white p-2 text-[#423800] shadow-[0_12px_28px_rgba(66,56,0,0.08)]"
                    onMouseEnter={cancelSubmenuClose}
                    onMouseLeave={scheduleSubmenuClose}
                    role="menu"
                    style={{ top: `calc(100% + ${4 + submenuTopOffset}px)` }}
                  >
                    <div className="mt-1 space-y-0.5">
                      {activeFilterSection === "content" && FILTER_OPTIONS.map((option) => {
                        const isSelected = selectedFilterTypes.includes(option.value);
                        return (
                          <button aria-pressed={isSelected} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-[#F7F7F7]" key={option.value} onClick={() => toggleFilterType(option.value)} type="button">
                            <span className="flex-1 font-geist text-[11px] font-medium">{option.label}</span>
                            <span className={`flex size-4 items-center justify-center rounded-full border ${isSelected ? "border-[#EDC800] bg-[#FED700]" : "border-[#D4D4D4]"}`}>{isSelected && <Check color="#423800" size={10} weight="bold" />}</span>
                          </button>
                        );
                      })}
                      {activeFilterSection === "duration" && DURATION_OPTIONS.map((option) => {
                        const isSelected = selectedDuration === option.value;
                        return (
                          <button aria-pressed={isSelected} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-[#F7F7F7]" key={option.value} onClick={() => { setSelectedDuration(option.value); setActiveFilterSection(null); }} type="button">
                            <span className="flex-1 font-geist text-[11px] font-medium">{option.label}</span>
                            {isSelected && <Check color="#EDC800" size={14} weight="bold" />}
                          </button>
                        );
                      })}
                      {activeFilterSection === "dimensions" && ORIENTATION_OPTIONS.map((option) => {
                        const isSelected = selectedOrientation === option.value;
                        return (
                          <button aria-pressed={isSelected} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-[#F7F7F7]" key={option.value} onClick={() => { setSelectedOrientation(option.value); setActiveFilterSection(null); }} type="button">
                            <span className="flex-1 font-geist text-[11px] font-medium">{option.label}</span>
                            {isSelected && <Check color="#EDC800" size={14} weight="bold" />}
                          </button>
                        );
                      })}
                      {activeFilterSection === "match" && MATCH_OPTIONS.map((option) => {
                        const isSelected = selectedMatchRange === option.value;
                        return (
                          <button aria-pressed={isSelected} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-[#F7F7F7]" key={option.value} onClick={() => { setSelectedMatchRange(option.value); setActiveFilterSection(null); }} type="button">
                            <span className="flex-1 font-geist text-[11px] font-medium">{option.label}</span>
                            {isSelected && <Check color="#EDC800" size={14} weight="bold" />}
                          </button>
                        );
                      })}
                      {activeFilterSection === "format" && availableFormats.map((format) => {
                        const isSelected = selectedFormats.includes(format);
                        return (
                          <button aria-pressed={isSelected} className="flex w-full cursor-pointer items-center gap-2 rounded-lg px-2 py-2 text-left hover:bg-[#F7F7F7]" key={format} onClick={() => setSelectedFormats((currentFormats) => isSelected ? currentFormats.filter((currentFormat) => currentFormat !== format) : [...currentFormats, format])} type="button">
                            <span className="flex-1 font-geist text-[11px] font-medium uppercase">{format}</span>
                            {isSelected && <Check color="#EDC800" size={14} weight="bold" />}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </>
            )}
            <button
              aria-expanded={isFiltersOpen}
              aria-label="Filters"
              className="flex h-8 cursor-pointer items-center justify-center gap-2 overflow-hidden rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 text-[#423800] transition-[border-width] duration-150 ease-out active:border-b"
              data-filters-trigger
              onClick={() => {
                cancelSubmenuClose();
                setIsFiltersOpen((isOpen) => !isOpen);
                setActiveFilterSection(null);
                cancelSeedMoodboardClose();
                setIsSeedDataMenuOpen(false);
                setIsSeedMoodboardMenuOpen(false);
              }}
              type="button"
            >
              <SlidersHorizontal color="#423800" size={12} weight="bold" />
              <span className="font-geist text-[12px] font-medium leading-none">Filters</span>
              {activeFilterCount > 0 && <span className="size-1.5 rounded-full bg-[#FED700]" />}
            </button>
          </div>
        </div>
        <div className="absolute bottom-4 right-4 flex items-end gap-2">
          <div className="relative" ref={examplesPopoverRef}>
            {isExamplesOpen && (
          <div className="absolute right-0 top-[calc(100%+4px)] z-30 w-60 rounded-2xl border border-[#E1E1E1] bg-[#F7F7F7] p-3 text-[#423800] shadow-[0_8px_20px_rgba(66,56,0,0.06)]">
              <div className="flex items-center justify-between gap-3">
                <input
                  aria-label="Number of examples"
                  className="h-7 w-16 appearance-none rounded-md border border-[#E1E1E1] bg-white px-2 text-center font-geist text-[12px] font-medium text-[#423800] outline-none focus:border-[#423800]"
                  onChange={(event) => {
                    setExampleCountInput(event.target.value);
                  }}
                  onBlur={(event) => {
                    const nextValue = Number(event.target.value);
                    const validatedValue = Number.isFinite(nextValue) ? clampExampleCount(nextValue) : MIN_EXAMPLES;
                    setExampleCount(validatedValue);
                    setExampleCountInput(String(validatedValue));
                  }}
                  step={100}
                  type="number"
                  value={exampleCountInput}
                />
                <span className="font-geist text-[12px] font-light text-[#777777]">
                  ~{estimatedMinMinutes} to {estimatedMaxMinutes} mins
                </span>
              </div>
              <div className="relative mt-5 h-5 px-1">
                <div className="absolute left-1 right-1 top-1/2 h-3 -translate-y-1/2 rounded-full bg-gradient-to-r from-[#E5E7EB] via-[#FDE68A] to-[#FED700]" />
                <div
                  className="absolute left-1 top-1/2 h-3 -translate-y-1/2 rounded-full"
                  style={{ backgroundColor: taskColor, width: `${exampleProgress * 100}%` }}
                />
                {[25, 50, 75].map((position) => (
                  <span
                    className="pointer-events-none absolute top-1/2 size-1 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white/90"
                    key={position}
                    style={{ left: `${position}%` }}
                  />
                ))}
                <input
                  aria-label="Number of examples"
                  className="absolute inset-0 h-5 w-full cursor-pointer opacity-0"
                  max={MAX_EXAMPLES}
                  min={MIN_EXAMPLES}
                  onChange={(event) => {
                    const nextValue = Number(event.target.value);
                    setExampleCount(nextValue);
                    setExampleCountInput(String(nextValue));
                  }}
                  step={1}
                  type="range"
                  value={exampleCount}
                />
                <span
                  className="pointer-events-none absolute top-1/2 size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-[#E1E1E1] bg-white shadow-sm"
                  style={{ left: `${exampleProgress * 100}%` }}
                />
              </div>
              <div className="mt-2 flex justify-between font-geist text-[10px] text-[#999999]">
                <span>{MIN_EXAMPLES}</span>
                <span>{MAX_EXAMPLES}</span>
              </div>
              </div>
            )}
            <button
              aria-label="Choose examples"
              aria-expanded={isExamplesOpen}
              className="flex h-8 cursor-pointer items-center justify-center gap-2 overflow-hidden rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 text-[#423800] transition-[border-width] duration-150 ease-out active:border-b"
              onClick={() => {
                setIsExamplesOpen((isOpen) => !isOpen);
                cancelSeedMoodboardClose();
                setIsSeedDataMenuOpen(false);
                setIsSeedMoodboardMenuOpen(false);
              }}
              type="button"
            >
              <span className="font-geist text-[12px] font-medium leading-none">{exampleCount} Examples</span>
              <CaretDown
                className={`transition-transform duration-150 ${isExamplesOpen ? "rotate-180" : ""}`}
                color="#423800"
                size={12}
                weight="bold"
              />
            </button>
          </div>
          <button
            aria-label="Submit prompt"
            aria-busy={isSubmitting}
            className="flex size-8 cursor-pointer items-center justify-center overflow-hidden rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] transition-[border-width,opacity] duration-150 ease-out active:border-b disabled:cursor-wait disabled:opacity-60"
            disabled={isSubmitting}
            onClick={submitRequest}
            type="button"
          >
            <ArrowUp
              color="#423800"
              size={16}
              weight="bold"
            />
          </button>
        </div>
      </div>
      {activeAttachment && (
        <div
          aria-label="Attachment preview"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
          onClick={() => setActiveAttachmentId(null)}
          role="dialog"
        >
          <div
            className="relative max-w-[90vw] rounded-2xl bg-white p-4 shadow-2xl"
            onClick={(event) => event.stopPropagation()}
          >
            <button
              aria-label="Close attachment preview"
              className="absolute right-3 top-3 z-10 flex size-8 cursor-pointer items-center justify-center rounded-full bg-[#F4F4F5] text-[#09090B] shadow-[0_8px_24px_rgba(9,9,11,0.16)] transition-colors hover:bg-[#EDEDEF]"
              onClick={() => setActiveAttachmentId(null)}
              type="button"
            >
              <X size={16} weight="regular" />
            </button>
            <FilePreview
              attachment={activeAttachment}
              className="h-[70vh] w-[min(80vw,720px)] rounded-lg object-contain"
            />
            <p className="mt-3 truncate font-geist text-sm font-medium text-[#423800]">
              {activeAttachment.file.name}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

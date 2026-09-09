"use client";

import Image from "next/image";
import { CaretDown, Check, DownloadSimple, Plus, ShareNetwork, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";

import HeaderNav from "@/components/header-nav";
import HeaderSearch from "@/components/header-search";
import PromptBox from "@/components/prompt-box";
import { REFERO_IMAGES } from "@/data/refero-images";
import type { DatasetReference } from "@/types/dataset";

const CATEGORY_SUGGESTIONS = [
  "Popular Categories",
  "Egocentric POV",
  "Digital Twin - Interiors",
  "Computer Use",
  "Medical Imaging",
  "3D Modelling",
  "Architectural Floor Plans",
  "Simulator Footage",
  "Egocentric - UMI Gripper",
  "Ad Creative Design",
  "Social Interactions",
  "Retail & Shopping",
  "Kitchen Activities",
  "Sports & Fitness",
  "Warehouse Robotics",
  "Healthcare Workflows",
  "Street Scenes",
  "Product Photography",
  "Industrial Inspection",
  "Agriculture",
  "Construction Sites",
  "Hand-Object Interaction",
  "Animation References",
  "Autonomous Driving",
  "Security Footage",
  "Natural Language UI",
];

const isVideoAsset = (src: string) => /\.(mp4|webm|mov)$/i.test(src);

export default function Home() {
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const exploreSectionRef = useRef<HTMLDivElement>(null);
  const toastTimeoutRef = useRef<number | null>(null);
  const [isSearchMode, setIsSearchMode] = useState(false);
  const [isExploreSettled, setIsExploreSettled] = useState(false);
  const [selectedCategory, setSelectedCategory] = useState(CATEGORY_SUGGESTIONS[0]);
  const [selectedMoodboardCategory, setSelectedMoodboardCategory] = useState<string | null>(null);
  const [isMoodboardCategoryOpen, setIsMoodboardCategoryOpen] = useState(false);
  const [selectedImages, setSelectedImages] = useState<Set<string>>(() => new Set());
  const [seedDataReferences, setSeedDataReferences] = useState<DatasetReference[]>([]);
  const [previewImage, setPreviewImage] = useState<(typeof REFERO_IMAGES)[number] | null>(null);
  const [isMoodboardOpen, setIsMoodboardOpen] = useState(false);
  const [isMoodboardMenuOpen, setIsMoodboardMenuOpen] = useState(false);
  const [moodboardName, setMoodboardName] = useState("");
  const [selectedMoodboard, setSelectedMoodboard] = useState<string | null>(null);
  const [savedMoodboards, setSavedMoodboards] = useState<string[]>([
    "Product Inspiration",
    "Research References",
    "Landing Page Patterns",
  ]);
  const [moodboardItems, setMoodboardItems] = useState<Record<string, DatasetReference[]>>({
    "Product Inspiration": REFERO_IMAGES.slice(0, 3),
    "Research References": REFERO_IMAGES.slice(3, 6),
    "Landing Page Patterns": REFERO_IMAGES.slice(6, 9),
  });
  const [isMoodboardNameFocused, setIsMoodboardNameFocused] = useState(false);
  const [toastMessage, setToastMessage] = useState<string | null>(null);

  useEffect(() => {
    const scrollContainer = scrollContainerRef.current;

    if (!scrollContainer) {
      return;
    }

    const updateSearchMode = () => {
      const exploreTop = Math.max(
        1,
        (exploreSectionRef.current?.offsetTop ?? scrollContainer.clientHeight) - 80,
      );
      const distanceFromExplore = exploreTop - scrollContainer.scrollTop;

      setIsSearchMode((isCurrentlyVisible) => {
        const threshold = exploreTop * (isCurrentlyVisible ? 0.55 : 0.72);
        return scrollContainer.scrollTop >= threshold;
      });
      setIsExploreSettled((isCurrentlySettled) =>
        isCurrentlySettled ? distanceFromExplore <= 24 : distanceFromExplore <= 2,
      );
    };

    updateSearchMode();
    scrollContainer.addEventListener("scroll", updateSearchMode, { passive: true });

    return () => scrollContainer.removeEventListener("scroll", updateSearchMode);
  }, []);

  useEffect(() => {
    if (!previewImage && !isMoodboardOpen) {
      return;
    }

    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPreviewImage(null);
        setIsMoodboardOpen(false);
      }
    };

    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [isMoodboardOpen, previewImage]);

  useEffect(() => {
    return () => {
      if (toastTimeoutRef.current !== null) {
        window.clearTimeout(toastTimeoutRef.current);
      }
    };
  }, []);

  const toggleImageSelection = (src: string) => {
    setSelectedImages((currentSelection) => {
      const nextSelection = new Set(currentSelection);

      if (nextSelection.has(src)) {
        nextSelection.delete(src);
      } else {
        nextSelection.add(src);
      }

      return nextSelection;
    });
  };

  const selectedFeedImages = Array.from(selectedImages)
    .map((src) => REFERO_IMAGES.find((image) => image.src === src))
    .filter((image): image is DatasetReference => image !== undefined);

  const removeSeedDataReference = (src: string) => {
    setSeedDataReferences((currentReferences) => currentReferences.filter((reference) => reference.src !== src));
  };

  const addSelectedToSeedData = () => {
    setSeedDataReferences((currentReferences) => {
      const referencesBySource = new Map(currentReferences.map((reference) => [reference.src, reference]));

      selectedFeedImages.forEach((reference) => referencesBySource.set(reference.src, reference));
      return Array.from(referencesBySource.values());
    });
    setSelectedImages(new Set<string>());
  };

  const downloadSelectedImages = () => {
    downloadImages(selectedFeedImages);
    setSelectedImages(new Set<string>());
  };

  const downloadImages = (images: DatasetReference[]) => {
    images.forEach((image, index) => {
      window.setTimeout(() => {
        const link = document.createElement("a");
        link.href = image.src;
        link.download = image.src.split("/").pop() ?? `dataset-reference-${index + 1}`;
        document.body.appendChild(link);
        link.click();
        link.remove();
      }, index * 120);
    });
  };

  const showToast = (message: string) => {
    setToastMessage(message);

    if (toastTimeoutRef.current !== null) {
      window.clearTimeout(toastTimeoutRef.current);
    }

    toastTimeoutRef.current = window.setTimeout(() => {
      setToastMessage(null);
      toastTimeoutRef.current = null;
    }, 3000);
  };

  const activeMoodboardItems = selectedMoodboardCategory ? moodboardItems[selectedMoodboardCategory] ?? [] : [];

  const downloadActiveMoodboardItems = () => {
    if (activeMoodboardItems.length === 0) {
      showToast(`${selectedMoodboardCategory ?? "Moodboard"} has no items`);
      return;
    }

    downloadImages(activeMoodboardItems);
    showToast(`Downloading ${activeMoodboardItems.length} items`);
  };

  const shareActiveMoodboard = () => {
    if (selectedMoodboardCategory) {
      if (navigator.clipboard) {
        void navigator.clipboard.writeText(window.location.href);
      }
      showToast(`Shared ${selectedMoodboardCategory}`);
    }
  };

  const handleSaveMoodboard = () => {
    const name = moodboardName.trim() || selectedMoodboard || "Untitled Moodboard";

    setSavedMoodboards((currentMoodboards) =>
      currentMoodboards.includes(name) ? currentMoodboards : [...currentMoodboards, name],
    );
    setMoodboardItems((currentMoodboardItems) => {
      const itemsBySource = new Map((currentMoodboardItems[name] ?? []).map((item) => [item.src, item]));
      selectedFeedImages.forEach((item) => itemsBySource.set(item.src, item));
      return { ...currentMoodboardItems, [name]: Array.from(itemsBySource.values()) };
    });
    setSelectedImages(new Set<string>());
    setSelectedMoodboardCategory(name);
    setSelectedCategory("");
    setIsMoodboardOpen(false);
    setIsMoodboardMenuOpen(false);
    setMoodboardName("");
    setSelectedMoodboard(null);
    setIsMoodboardNameFocused(false);
    showToast(`Saved to ${name}`);
  };

  const scrollToExplore = () => {
    const scrollContainer = scrollContainerRef.current;
    const exploreSection = exploreSectionRef.current;

    if (!scrollContainer || !exploreSection) {
      return;
    }

    const targetTop = Math.max(0, exploreSection.offsetTop - 80);
    const prefersReducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    if (Math.abs(scrollContainer.scrollTop - targetTop) < 2) {
      return;
    }

    scrollContainer.scrollTo({
      top: targetTop,
      behavior: prefersReducedMotion ? "auto" : "smooth",
    });
  };

  const selectMoodboardCategory = (name: string) => {
    setSelectedMoodboardCategory(name);
    setSelectedCategory("");
    setIsMoodboardCategoryOpen(false);
    scrollToExplore();
  };

  const selectCategory = (category: string) => {
    setSelectedCategory(category);
    setSelectedMoodboardCategory(null);
    setIsMoodboardCategoryOpen(false);
    scrollToExplore();
  };

  return (
    <div className="relative h-screen snap-y snap-proximity scroll-pt-20 overflow-y-auto overscroll-y-contain" ref={scrollContainerRef}>
      <header className="fixed inset-x-0 top-0 z-50 h-16 bg-white">
        <Image
          alt="Ola Amigo"
          className="absolute left-4 top-4 h-8 w-auto"
          height={32}
          priority
          src="/logo.png"
          width={142}
        />
        <div className={`transition-opacity duration-300 ${isSearchMode ? "pointer-events-none opacity-0" : "opacity-100"}`}>
          <HeaderNav />
        </div>
        <button
          className="absolute right-4 top-4 flex h-8 cursor-pointer items-center justify-center rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-4 font-geist text-[12px] font-medium text-[#423800] transition-[border-width,background-color] duration-150 ease-out hover:bg-[#F1F1F1] active:border-b"
          type="button"
        >
          Get Started
        </button>
        {selectedMoodboardCategory && (
          <div className="absolute right-[132px] top-4 flex h-8 items-center gap-2">
            <button
              aria-label={`Share ${selectedMoodboardCategory}`}
              className="flex h-8 cursor-pointer items-center gap-1.5 rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-3 font-geist text-[11px] font-medium text-[#423800] transition-[border-width,background-color] hover:bg-[#F1F1F1] active:border-b"
              onClick={shareActiveMoodboard}
              type="button"
            >
              <ShareNetwork size={13} weight="regular" />
              Share Moodboard
            </button>
            <button
              aria-label={`Download items from ${selectedMoodboardCategory}`}
              className="flex h-8 cursor-pointer items-center gap-1.5 rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] px-3 font-geist text-[11px] font-medium text-[#423800] transition-[border-width,background-color] hover:bg-[#F1F1F1] active:border-b"
              onClick={downloadActiveMoodboardItems}
              type="button"
            >
              <DownloadSimple size={13} weight="regular" />
              Download Items
            </button>
          </div>
        )}
        <HeaderSearch isVisible={isSearchMode} />
      </header>
      <main className="relative z-10 grid min-h-screen snap-start place-items-center">
        <div className="relative">
          <div className="absolute bottom-full left-1/2 mb-16 w-max -translate-x-1/2 text-center">
            <h1 className="font-fuzzy-bubbles text-[56px] font-bold leading-[1.1] tracking-[-0.1em] text-[#282828]">
              Teach your AI with data
            </h1>
            <p className="mt-2 font-geist text-[16px] font-light leading-6 tracking-[0] text-[#A3A3A3]">
              Discover high quality data, build datasets, and train models, all from one place.
            </p>
          </div>
          <PromptBox
            moodboards={savedMoodboards}
            onRemoveSeedDataReference={removeSeedDataReference}
            onSelectSeedMoodboard={(name) => setSeedDataReferences(moodboardItems[name] ?? [])}
            seedDataReferences={seedDataReferences}
          />
        </div>
      </main>
      <div
        className="relative mx-4 -mt-[200px] mb-4 snap-start"
        ref={exploreSectionRef}
        onMouseDown={(event) => {
          if (isMoodboardCategoryOpen && !(event.target as Element).closest("[data-moodboard-category]")) {
            setIsMoodboardCategoryOpen(false);
          }
        }}
      >
        <div className="sticky top-20 z-30 mb-2 flex h-7 min-w-0 items-center overflow-visible bg-white" role="tablist" aria-label="Dataset categories">
          <div className="flex h-7 min-w-0 items-center">
            <div className="relative flex h-7 shrink-0 items-center rounded-lg bg-[#F4F4F5] p-0.5" data-moodboard-category>
              <button
                aria-expanded={isMoodboardCategoryOpen}
                aria-haspopup="menu"
                className={`box-border flex h-6 min-h-6 max-w-[220px] cursor-pointer items-center gap-1 overflow-hidden rounded-md px-3 font-geist text-[12px] leading-4 tracking-[-0.01em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#09090B] ${
                  selectedMoodboardCategory
                    ? "bg-white font-medium text-[#09090B] shadow-sm"
                    : "bg-[#F4F4F5] font-normal text-[#71717A] hover:text-[#09090B]"
                }`}
                onClick={() => setIsMoodboardCategoryOpen((isOpen) => !isOpen)}
                type="button"
              >
                <span className="min-w-0 truncate">{selectedMoodboardCategory ?? "Your Moodboard"}</span>
                <CaretDown className={`shrink-0 transition-transform ${isMoodboardCategoryOpen ? "rotate-180" : ""}`} size={14} weight="regular" />
              </button>
              {isMoodboardCategoryOpen && (
                <div
                  className="absolute left-0 top-[calc(100%+4px)] z-40 min-w-[210px] rounded-xl border border-[#E1E1E1] bg-white p-1.5 shadow-[0_12px_28px_rgba(9,9,11,0.12)]"
                  role="menu"
                >
                  {savedMoodboards.map((name) => (
                    <button
                      className="flex w-full cursor-pointer items-center rounded-lg px-3 py-2 text-left font-geist text-[12px] font-medium text-[#09090B] transition-colors hover:bg-[#F4F4F5]"
                      key={name}
                      onClick={() => selectMoodboardCategory(name)}
                      role="menuitem"
                      type="button"
                    >
                      {name}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <span aria-hidden="true" className="mx-1 h-4 w-px shrink-0 bg-[#E1E1E1]" />
            <div className="min-w-0 flex-1 overflow-x-auto overscroll-x-contain">
              <div className="flex h-7 w-max items-center gap-2">
                {CATEGORY_SUGGESTIONS.map((category) => (
                  <div className="flex h-7 shrink-0 items-center rounded-lg bg-[#F4F4F5] p-0.5" key={category}>
                    <button
                      aria-selected={selectedCategory === category}
                      className={`h-6 rounded-md border border-transparent px-3 font-geist text-[12px] tracking-[-0.01em] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#09090B] ${
                        selectedCategory === category
                          ? "bg-white font-medium text-[#09090B] shadow-sm"
                          : "bg-transparent font-normal text-[#71717A] hover:text-[#09090B]"
                      }`}
                      onClick={() => selectCategory(category)}
                      role="tab"
                      type="button"
                    >
                      {category}
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
        <section
          aria-label="Dataset feed"
          className={`sticky top-28 z-10 h-[calc(100vh-128px)] rounded-lg border border-[#E1E1E1] bg-white ${
            isExploreSettled ? "overflow-y-auto overscroll-y-auto" : "overflow-y-hidden"
          }`}
        >
          <div className="grid grid-cols-1 gap-2 p-2 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {REFERO_IMAGES.map((image, index) => (
              <article
                className={`group relative aspect-[4/3] overflow-hidden rounded-lg border bg-[#F4F4F5] transition-[border-color,box-shadow] ${
                  selectedImages.has(image.src) ? "border-[#423800] ring-2 ring-[#423800]/15" : "border-[#E4E4E7]"
                }`}
                key={image.src}
              >
                <button
                  aria-label={`Preview ${image.alt}`}
                  className="absolute inset-0 z-0 block h-full w-full cursor-pointer overflow-hidden"
                  onClick={() => setPreviewImage(image)}
                  type="button"
                >
                  {isVideoAsset(image.src) ? (
                    <video
                      aria-label={image.alt}
                      className="h-full w-full object-cover"
                      loop
                      muted
                      onMouseEnter={(event) => void event.currentTarget.play()}
                      onMouseLeave={(event) => {
                        event.currentTarget.pause();
                        event.currentTarget.currentTime = 0;
                      }}
                      playsInline
                      preload="metadata"
                    >
                      <source src={image.src} />
                    </video>
                  ) : (
                    <Image
                      alt={image.alt}
                      className="animate-feed-pan object-cover"
                      fill
                      loading={index < 4 ? "eager" : "lazy"}
                      sizes="(min-width: 1280px) 25vw, (min-width: 1024px) 33vw, (min-width: 640px) 50vw, 100vw"
                      src={image.src}
                    />
                  )}
                </button>
                <button
                  aria-label={`${selectedImages.has(image.src) ? "Deselect" : "Select"} ${image.alt}`}
                  aria-pressed={selectedImages.has(image.src)}
                  className={`absolute right-2 top-2 z-10 flex h-6 w-6 items-center justify-center rounded-full border border-[#E4E4E7] bg-white text-[#09090B] opacity-0 transition-[opacity,background-color] group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#09090B] hover:bg-[#F4F4F5] ${
                    selectedImages.has(image.src) ? "opacity-100" : ""
                  }`}
                  onClick={() => toggleImageSelection(image.src)}
                  type="button"
                >
                  {selectedImages.has(image.src) ? <Check size={13} weight="bold" /> : null}
                </button>
              </article>
            ))}
          </div>
        </section>
      </div>
      {selectedFeedImages.length > 0 && (
        <div className="group/selection fixed bottom-4 left-4 z-40 flex flex-col items-start gap-2">
          <div className="relative h-[150px] w-[194px]">
            {selectedFeedImages.slice(-3).reverse().map((image, index) => (
              <div
                className={`absolute top-0 size-[150px] overflow-hidden rounded-2xl border-2 border-white bg-[#F4F4F5] shadow-[0_8px_20px_rgba(66,56,0,0.14)] transition-[filter,opacity,transform] duration-200 group-hover/selection:scale-[1.02] ${index > 0 ? "grayscale-[0.8] opacity-70" : ""}`}
                key={image.src}
                style={{ left: `${index * 16}px`, zIndex: 3 - index, transform: `rotate(${(index - 1) * 2}deg)` }}
              >
                <Image
                  alt={image.alt}
                  className="object-cover"
                  fill
                  sizes="150px"
                  src={image.src}
                />
              </div>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <button
              aria-haspopup="dialog"
              className="flex h-9 cursor-pointer items-center rounded-full border border-[#09090B] bg-[#09090B] px-3 font-geist text-[11px] font-medium text-white shadow-[0_6px_16px_rgba(9,9,11,0.16)] transition-colors hover:bg-[#27272A]"
              onClick={() => {
                setIsMoodboardOpen(true);
                setIsMoodboardMenuOpen(false);
              }}
              type="button"
            >
              Save {selectedFeedImages.length} {selectedFeedImages.length === 1 ? "Item" : "Items"}
            </button>
            <div className="pointer-events-none flex items-center gap-1 opacity-0 transition-opacity duration-150 group-hover/selection:pointer-events-auto group-hover/selection:opacity-100">
              <button
                aria-label="Download selected items"
                className="flex h-9 cursor-pointer items-center gap-1 rounded-full border border-[#E1E1E1] bg-white px-3 font-geist text-[11px] font-medium text-[#423800] shadow-[0_6px_16px_rgba(66,56,0,0.12)] transition-colors hover:bg-[#F4F4F5]"
                onClick={downloadSelectedImages}
                type="button"
              >
                <DownloadSimple size={13} weight="bold" />
                Download
              </button>
              <button
                aria-label="Add selected items to seed data"
                className="flex h-9 cursor-pointer items-center gap-1 rounded-full border border-[#EDC800] bg-[#FED700] px-3 font-geist text-[11px] font-medium text-[#423800] shadow-[0_6px_16px_rgba(66,56,0,0.12)] transition-colors hover:bg-[#F5CE00]"
                onClick={addSelectedToSeedData}
                type="button"
              >
                <Plus size={13} weight="bold" />
                Add to Seed Data
              </button>
            </div>
          </div>
        </div>
      )}
      {isMoodboardOpen && (
        <div
          aria-label="Select moodboard"
          aria-modal="true"
          className="fixed inset-0 z-[90] flex items-center justify-center bg-[#09090B]/20 p-4 backdrop-blur-[2px]"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setIsMoodboardOpen(false);
            }
          }}
          role="dialog"
        >
          <div
            className="w-[min(560px,calc(100vw-32px))] rounded-[24px] border border-[#E1E1E1] bg-white p-6 text-[#09090B] shadow-[0_20px_60px_rgba(9,9,11,0.16)]"
            onMouseDown={(event) => {
              if (isMoodboardMenuOpen && !(event.target as Element).closest("[data-moodboard-menu]")) {
                setIsMoodboardMenuOpen(false);
              }
            }}
          >
            <div className="flex items-center justify-between gap-4">
              <div className="relative" data-moodboard-menu>
                <button
                  aria-expanded={isMoodboardMenuOpen}
                  aria-haspopup="menu"
                  className="flex cursor-pointer items-center gap-1.5 font-geist text-[18px] font-medium tracking-[-0.02em] text-[#09090B]"
                  onClick={() => setIsMoodboardMenuOpen((isOpen) => !isOpen)}
                  type="button"
                >
                  Select Moodboard
                  <CaretDown className={`transition-transform ${isMoodboardMenuOpen ? "rotate-180" : ""}`} size={16} weight="regular" />
                </button>
                {isMoodboardMenuOpen && (
                  <div
                    className="absolute left-0 top-[calc(100%+8px)] z-20 min-w-[220px] rounded-xl border border-[#E1E1E1] bg-white p-1.5 shadow-[0_12px_28px_rgba(9,9,11,0.12)]"
                    role="menu"
                  >
                    <button
                      className="flex w-full cursor-pointer items-center rounded-lg px-3 py-2 text-left font-geist text-[12px] font-medium text-[#09090B] transition-colors hover:bg-[#F4F4F5]"
                      onClick={() => {
                        setMoodboardName("");
                        setSelectedMoodboard(null);
                        setIsMoodboardMenuOpen(false);
                      }}
                      role="menuitem"
                      type="button"
                    >
                      New Moodboard
                    </button>
                    {savedMoodboards.map((name) => (
                      <button
                        className="flex w-full cursor-pointer items-center rounded-lg px-3 py-2 text-left font-geist text-[12px] font-medium text-[#09090B] transition-colors hover:bg-[#F4F4F5]"
                        key={name}
                        onClick={() => {
                          setMoodboardName(name);
                          setSelectedMoodboard(name);
                          setIsMoodboardMenuOpen(false);
                        }}
                        role="menuitem"
                        type="button"
                      >
                        {name}
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <button
                aria-label="Close moodboard dialog"
                className="flex size-8 cursor-pointer items-center justify-center rounded-full bg-[#F4F4F5] text-[#09090B] transition-colors hover:bg-[#EDEDEF]"
                onClick={() => {
                  setIsMoodboardOpen(false);
                  setIsMoodboardMenuOpen(false);
                }}
                type="button"
              >
                <X size={18} weight="regular" />
              </button>
            </div>
            <div className="py-10">
              <input
                aria-label="Moodboard name"
                autoFocus
                className="w-full border-0 bg-transparent text-center font-geist text-[32px] font-medium tracking-[-0.04em] text-[#09090B] outline-none placeholder:text-[#A1A1AA]"
                onBlur={() => setIsMoodboardNameFocused(false)}
                onChange={(event) => setMoodboardName(event.target.value)}
                onFocus={() => setIsMoodboardNameFocused(true)}
                placeholder={isMoodboardNameFocused ? "" : "Untitled Moodboard"}
                value={moodboardName}
              />
            </div>
            <button
              className="flex h-12 w-full cursor-pointer items-center justify-center rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] font-geist text-[14px] font-medium text-[#423800] transition-colors hover:bg-[#F5CE00] active:border-b"
              onClick={handleSaveMoodboard}
              type="button"
            >
              Save to Moodboard
            </button>
          </div>
        </div>
      )}
      {toastMessage && (
        <div
          aria-live="polite"
          className="fixed bottom-6 left-1/2 z-[120] -translate-x-1/2 rounded-lg border border-[#27272A] bg-[#09090B] px-4 py-2.5 font-geist text-[12px] font-medium text-white shadow-[0_8px_24px_rgba(9,9,11,0.2)]"
          role="status"
        >
          {toastMessage}
        </div>
      )}
      {previewImage ? (
        <div
          aria-label={`Preview of ${previewImage.alt}`}
          aria-modal="true"
          className="fixed inset-0 z-[100] flex items-center justify-center bg-[#09090B]/70 p-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setPreviewImage(null);
            }
          }}
          role="dialog"
        >
          <button
            aria-label="Close preview"
            className="absolute right-6 top-6 z-10 flex size-8 cursor-pointer items-center justify-center rounded-full bg-[#F4F4F5] text-[#09090B] shadow-[0_8px_24px_rgba(9,9,11,0.16)] transition-colors hover:bg-[#EDEDEF]"
            onClick={() => setPreviewImage(null)}
            type="button"
          >
            <X size={18} weight="regular" />
          </button>
          <div
            className="relative h-[min(86vh,860px)] w-[min(94vw,1240px)]"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) {
                setPreviewImage(null);
              }
            }}
          >
            {isVideoAsset(previewImage.src) ? (
              <video
                aria-label={previewImage.alt}
                autoPlay
                className="h-full w-full object-contain"
                controls
                loop
                muted
                playsInline
                src={previewImage.src}
              />
              ) : (
                <Image alt={previewImage.alt} className="object-contain" fill sizes="94vw" src={previewImage.src} />
              )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

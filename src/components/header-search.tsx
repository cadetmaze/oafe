"use client";

import { MagnifyingGlass } from "@phosphor-icons/react";
import { useEffect, useState } from "react";

import { DATASET_EXAMPLES } from "@/components/prompt-box";

export default function HeaderSearch({ isVisible }: { isVisible: boolean }) {
  const [exampleIndex, setExampleIndex] = useState(0);
  const [placeholder, setPlaceholder] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);

  useEffect(() => {
    if (!isVisible) {
      return;
    }

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
  }, [exampleIndex, isDeleting, isVisible, placeholder]);

  return (
    <div
      aria-hidden={!isVisible}
      className={`absolute left-1/2 top-2.5 flex h-11 w-[min(600px,calc(100vw-240px))] -translate-x-1/2 rounded-2xl border border-[#E1E1E1] bg-[#F7F7F7] p-0.5 transition-[opacity,transform] duration-300 ease-out ${
        isVisible ? "scale-100 opacity-100" : "pointer-events-none scale-95 opacity-0"
      }`}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2 rounded-[12px] border border-dashed border-[#EEEEEE] bg-white px-3 font-geist text-[13px] font-light text-[#989898]">
        <MagnifyingGlass color="#989898" size={15} weight="regular" />
        <span className="min-w-0 flex-1 truncate whitespace-nowrap">{placeholder}</span>
      </div>
    </div>
  );
}

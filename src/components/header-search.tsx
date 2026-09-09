"use client";

import { MagnifyingGlass } from "@phosphor-icons/react";
import { useEffect, useState } from "react";

import { DATASET_EXAMPLES } from "@/components/prompt-box";

type HeaderSearchProps = {
  isSubmitting?: boolean;
  isVisible: boolean;
  onQueryChange: (query: string) => void;
  onSearch?: (query: string) => void;
  query: string;
};

export default function HeaderSearch({
  isSubmitting = false,
  isVisible,
  onQueryChange,
  onSearch,
  query,
}: HeaderSearchProps) {
  const [exampleIndex, setExampleIndex] = useState(0);
  const [placeholder, setPlaceholder] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const [isFocused, setIsFocused] = useState(false);

  useEffect(() => {
    if (!isVisible || isFocused || query.length > 0) {
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
  }, [exampleIndex, isDeleting, isFocused, isVisible, placeholder, query]);

  return (
    <form
      aria-busy={isSubmitting}
      aria-hidden={!isVisible}
      className={`absolute left-1/2 top-2.5 flex h-11 w-[min(600px,calc(100vw-240px))] -translate-x-1/2 rounded-2xl border border-[#E1E1E1] bg-[#F7F7F7] p-0.5 transition-[opacity,transform] duration-300 ease-out ${
        isVisible ? "scale-100 opacity-100" : "pointer-events-none scale-95 opacity-0"
      }`}
      onSubmit={(event) => {
        event.preventDefault();
        const normalizedQuery = query.trim();

        if (normalizedQuery && !isSubmitting) {
          onSearch?.(normalizedQuery);
        }
      }}
    >
      <div className="flex min-w-0 flex-1 cursor-text items-center gap-2 rounded-[12px] border border-dashed border-[#EEEEEE] bg-white px-3">
        <MagnifyingGlass className="shrink-0 text-[#989898]" size={15} weight="regular" />
        <input
          aria-label="Search datasets"
          autoComplete="off"
          className="min-w-0 flex-1 bg-transparent font-geist text-[13px] font-light text-[#282828] outline-none placeholder:text-[#989898]"
          disabled={!isVisible}
          onBlur={() => setIsFocused(false)}
          onChange={(event) => onQueryChange(event.target.value)}
          onFocus={() => setIsFocused(true)}
          placeholder={isFocused ? "" : placeholder}
          spellCheck={false}
          type="text"
          value={query}
        />
      </div>
    </form>
  );
}

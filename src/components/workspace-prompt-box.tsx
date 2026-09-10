"use client";

import { ArrowUp, Plus } from "@phosphor-icons/react";
import { useRef, useState } from "react";

type WorkspacePromptBoxProps = {
  disabled?: boolean;
  onSubmit?: (value: string) => void;
  onValueChange?: (value: string) => void;
  value?: string;
};

export default function WorkspacePromptBox({
  disabled = false,
  onSubmit,
  onValueChange,
  value,
}: WorkspacePromptBoxProps) {
  const [draft, setDraft] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const promptInputRef = useRef<HTMLTextAreaElement>(null);
  const promptValue = value ?? draft;

  function updatePromptValue(nextValue: string) {
    if (value === undefined) {
      setDraft(nextValue);
    }
    onValueChange?.(nextValue);
  }

  function submitPrompt() {
    const submittedValue = promptValue.trim();

    if (disabled || submittedValue.length === 0) {
      return;
    }

    onSubmit?.(submittedValue);
    updatePromptValue("");
  }

  return (
    <div
      className="h-full rounded-[18px] border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] p-[5px]"
      onClick={(event) => {
        if (!(event.target as Element).closest("button, input, textarea")) {
          promptInputRef.current?.focus();
        }
      }}
    >
      <input
        ref={fileInputRef}
        aria-hidden="true"
        className="hidden"
        multiple
        tabIndex={-1}
        type="file"
      />
      <div className="relative h-full rounded-[13px] bg-white">
        <svg
          aria-hidden="true"
          className="pointer-events-none absolute -inset-px h-[calc(100%+2px)] w-[calc(100%+2px)]"
          preserveAspectRatio="none"
          viewBox="0 0 600 136"
        >
          <rect
            fill="white"
            height="135"
            rx="13"
            stroke="#EEEEEE"
            strokeDasharray="8 8"
            strokeWidth="1"
            width="599"
            x="0.5"
            y="0.5"
          />
        </svg>
        <textarea
          ref={promptInputRef}
          aria-label="Prompt message"
          className="absolute inset-x-3 bottom-12 top-3 resize-none overflow-y-auto border-0 bg-transparent p-0 font-geist text-[14px] font-normal leading-5 text-[#282828] focus:outline-none"
          disabled={disabled}
          onChange={(event) => updatePromptValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              submitPrompt();
            }
          }}
          placeholder="What's the next step"
          value={promptValue}
        />
        <button
          aria-label="Add files"
          className="absolute bottom-3 left-3 flex size-8 items-center justify-center rounded-lg border border-[#E1E1E1] border-b-2 bg-[#F7F7F7] text-[#423800] transition-[border-width,background-color] hover:bg-[#F1F1F1] active:border-b"
          onClick={() => fileInputRef.current?.click()}
          type="button"
        >
          <Plus size={13} weight="bold" />
        </button>
        <button
          aria-label="Send prompt"
          className="absolute bottom-3 right-3 flex size-8 items-center justify-center rounded-lg border border-[#EDC800] border-b-2 bg-[#FED700] text-[#423800] transition-[border-width,opacity] active:border-b disabled:cursor-default disabled:opacity-45"
          disabled={disabled || promptValue.trim().length === 0}
          onClick={submitPrompt}
          type="button"
        >
          <ArrowUp size={16} weight="bold" />
        </button>
      </div>
    </div>
  );
}

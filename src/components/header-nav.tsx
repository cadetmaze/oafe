"use client";

import { ArrowUpRight } from "@phosphor-icons/react";
import { useState } from "react";

type NavItem = "Build Your Dataset" | "Training Suite" | "Pricing" | "Read Docs";

export default function HeaderNav() {
  const [selectedItem, setSelectedItem] = useState<NavItem>("Build Your Dataset");
  const itemClassName = (item: NavItem) =>
    `cursor-pointer transition-colors ${
      selectedItem === item ? "font-medium text-[#423800]" : "font-normal text-[#989898]"
    }`;

  return (
    <nav className="absolute left-1/2 top-4 flex h-8 -translate-x-1/2 items-center gap-8 font-geist text-[12px]">
      <button
        aria-pressed={selectedItem === "Build Your Dataset"}
        className={itemClassName("Build Your Dataset")}
        onClick={() => setSelectedItem("Build Your Dataset")}
        type="button"
      >
        Build Your Dataset
      </button>
      <button
        aria-pressed={selectedItem === "Training Suite"}
        className={`flex items-center gap-1.5 ${itemClassName("Training Suite")}`}
        onClick={() => setSelectedItem("Training Suite")}
        type="button"
      >
        Training Suite
        <span className="rounded-[4px] border border-[#E1E1E1] bg-[#F7F7F7] px-1 py-0.5 text-[8px] font-medium leading-none tracking-[0.04em] text-[#989898]">
          SOON
        </span>
      </button>
      <button
        aria-pressed={selectedItem === "Pricing"}
        className={itemClassName("Pricing")}
        onClick={() => setSelectedItem("Pricing")}
        type="button"
      >
        Pricing
      </button>
      <button
        aria-pressed={selectedItem === "Read Docs"}
        className={`flex items-center gap-1 ${itemClassName("Read Docs")}`}
        onClick={() => setSelectedItem("Read Docs")}
        type="button"
      >
        Read Docs
        <ArrowUpRight color="currentColor" size={12} weight="regular" />
      </button>
    </nav>
  );
}

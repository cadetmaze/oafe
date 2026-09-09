"use client";

import { ArrowUpRight } from "@phosphor-icons/react";

export default function HeaderNav() {
  return (
    <nav className="absolute left-1/2 top-4 flex h-8 -translate-x-1/2 items-center gap-8 font-geist text-[12px] font-medium">
      <button className="cursor-pointer text-[#423800]" type="button">
        Build Your Dataset
      </button>
      <button className="cursor-pointer text-[#989898]" type="button">
        Training Suite
      </button>
      <button className="cursor-pointer text-[#989898]" type="button">
        Pricing
      </button>
      <button className="flex cursor-pointer items-center gap-1 text-[#989898]" type="button">
        Read Docs
        <ArrowUpRight color="#989898" size={12} weight="regular" />
      </button>
    </nav>
  );
}

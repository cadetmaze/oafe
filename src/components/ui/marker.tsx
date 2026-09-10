import type { ComponentProps } from "react";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "@/lib/utils";

const markerVariants = cva(
  "group/marker relative flex min-h-4 w-full items-center gap-2 text-left font-geist text-xs text-[#71717A] [&_a]:underline [&_a]:underline-offset-3 [&_a:hover]:text-[#18181B] [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        default: "",
        border: "border-b border-[#E1E1E1] pb-2",
        separator:
          "before:mr-1 before:h-px before:min-w-0 before:flex-1 before:bg-[#E1E1E1] after:ml-1 after:h-px after:min-w-0 after:flex-1 after:bg-[#E1E1E1]",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

function Marker({
  className,
  variant = "default",
  ...props
}: ComponentProps<"div"> & VariantProps<typeof markerVariants>) {
  return (
    <div
      data-slot="marker"
      data-variant={variant}
      className={cn(markerVariants({ variant }), className)}
      {...props}
    />
  );
}

function MarkerIcon({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      data-slot="marker-icon"
      aria-hidden="true"
      className={cn(
        "size-4 shrink-0 [&_svg:not([class*='size-'])]:size-4",
        className,
      )}
      {...props}
    />
  );
}

function MarkerContent({ className, ...props }: ComponentProps<"span">) {
  return (
    <span
      data-slot="marker-content"
      className={cn(
        "min-w-0 break-words group-data-[variant=separator]/marker:flex-none group-data-[variant=separator]/marker:text-center [&_a]:underline [&_a]:underline-offset-3 [&_a:hover]:text-[#18181B]",
        className,
      )}
      {...props}
    />
  );
}

export { Marker, MarkerContent, MarkerIcon, markerVariants };

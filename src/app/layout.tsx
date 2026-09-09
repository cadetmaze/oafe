import type { Metadata } from "next";
import { Fuzzy_Bubbles, Geist } from "next/font/google";
import "./globals.css";

const fuzzyBubbles = Fuzzy_Bubbles({
  display: "swap",
  subsets: ["latin"],
  variable: "--fuzzy-bubbles",
  weight: "700",
});

const geist = Geist({
  display: "swap",
  subsets: ["latin"],
  variable: "--geist",
  weight: ["300", "400", "500"],
});

export const metadata: Metadata = {
  title: "OAFE",
  description: "Discover high quality data and build datasets for AI.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body className={`${geist.variable} ${fuzzyBubbles.variable}`}>{children}</body>
    </html>
  );
}

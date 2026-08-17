import "@fontsource/instrument-serif/400.css";
import { GeistSans } from "geist/font/sans";
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MIRRA — Wear the campaign",
  description: "Turn a fashion campaign into a personal, shoppable mirror.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" data-scroll-behavior="smooth">
      <body className={GeistSans.variable}>{children}</body>
    </html>
  );
}

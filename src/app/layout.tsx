import type { Metadata, Viewport } from "next";
import { Bricolage_Grotesque, Geist, Geist_Mono } from "next/font/google";
import { SiteFooter } from "@/components/layout/SiteFooter";
import { SiteHeader } from "@/components/layout/SiteHeader";
import { getStories } from "@/lib/api";
import "./globals.css";

const display = Bricolage_Grotesque({ subsets: ["latin"], variable: "--font-display", display: "swap" });
const body = Geist({ subsets: ["latin"], variable: "--font-body", display: "swap" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL;

export const metadata: Metadata = {
  ...(siteUrl ? { metadataBase: new URL(siteUrl), alternates: { canonical: "/" } } : {}),
  title: {
    default: "AI//CAMPUS — AI intelligence bulletin for DBS Global University students",
    template: "%s · AI//CAMPUS",
  },
  description:
    "A weekly, source-linked AI intelligence bulletin designed for the DBS Global University community: what changed in AI, why it matters, and what students can do next.",
  applicationName: "AI//CAMPUS",
  openGraph: {
    type: "website",
    siteName: "AI//CAMPUS",
    title: "AI//CAMPUS — The future isn’t waiting.",
    description: "AI news that helps students understand what changed, why it matters, and what to do next.",
    locale: "en_IN",
  },
  twitter: {
    card: "summary_large_image",
    title: "AI//CAMPUS",
    description: "AI news that helps students understand what changed, why it matters, and what to do next.",
  },
};

export const viewport: Viewport = {
  themeColor: "#170a1b",
  colorScheme: "light",
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const stories = await getStories();
  const searchIndex = stories.ok ? stories.data : [];

  return (
    <html lang="en-IN" className={`${display.variable} ${body.variable} ${mono.variable}`}>
      <body>
        <a
          href="#main"
          className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-[100] focus:rounded-full focus:bg-yellow focus:px-4 focus:py-2 focus:text-ink"
        >
          Skip to content
        </a>
        <SiteHeader searchIndex={searchIndex} />
        {children}
        <SiteFooter />
      </body>
    </html>
  );
}

import type { Metadata } from "next";
import Link from "next/link";

import "./globals.css";

export const metadata: Metadata = {
  title: "AgentOps — Multi-Agent Pipeline Observability",
  description:
    "Watch a hand-written multi-agent LLM pipeline execute in real time. No agent framework — raw API calls only.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-900 text-slate-100 antialiased">
        <header className="border-b border-slate-800 bg-slate-950">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-4 py-3">
            <Link href="/" className="flex items-baseline gap-2">
              <span className="text-lg font-semibold tracking-tight">
                Agent<span className="text-blue-400">Ops</span>
              </span>
              <span className="hidden text-xs text-slate-500 sm:inline">
                multi-agent pipeline observability
              </span>
            </Link>
            <span className="rounded border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400">
              no agent framework · raw LLM calls
            </span>
          </div>
        </header>

        <main className="mx-auto max-w-6xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}

/**
 * Run Detail View (TECH_SPEC §7.1).
 *
 * Server Component: fetches the job and its log history, then hands both to a
 * Client Component that opens the WebSocket and takes over live updates.
 */

import Link from "next/link";
import { notFound } from "next/navigation";

import { RunDetailLive } from "@/components/RunDetailLive";
import { ApiError, fetchJobServer, fetchLogsServer } from "@/lib/api";

export const dynamic = "force-dynamic";

interface PageProps {
  // Next.js 15 passes route params as a Promise.
  params: Promise<{ jobId: string }>;
}

export default async function RunDetailPage({ params }: PageProps) {
  const { jobId } = await params;

  try {
    const [job, logPage] = await Promise.all([
      fetchJobServer(jobId),
      fetchLogsServer(jobId),
    ]);

    return (
      <div className="space-y-4">
        <Link
          href="/"
          className="inline-block text-xs text-slate-400 hover:text-slate-200"
        >
          ← Back to dashboard
        </Link>
        <RunDetailLive initialJob={job} initialLogs={logPage.logs} />
      </div>
    );
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 404) {
      notFound();
    }
    return (
      <div className="space-y-4">
        <Link
          href="/"
          className="inline-block text-xs text-slate-400 hover:text-slate-200"
        >
          ← Back to dashboard
        </Link>
        <p
          role="alert"
          className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200"
        >
          Could not load this run. Check that the backend API is reachable.
        </p>
      </div>
    );
  }
}

/**
 * Dashboard (TECH_SPEC §7.1): submission form + live Job Queue View.
 *
 * A Server Component: the initial job list is fetched on the server, so the
 * first paint already has data. The interactive pieces are separate Client
 * Components below it.
 */

import { JobQueueList } from "@/components/JobQueueList";
import { JobSubmitForm } from "@/components/JobSubmitForm";
import { fetchJobsServer, type JobListResponse } from "@/lib/api";

// The queue is live state; never serve it from the full-route cache.
export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  let page: JobListResponse = { jobs: [], total: 0 };
  let loadError: string | null = null;

  try {
    page = await fetchJobsServer({ limit: 25 });
  } catch {
    // The dashboard must still render if the backend is down — the form and an
    // explanatory banner are more useful than a crashed route.
    loadError =
      "Could not reach the backend to load run history. Check that the API and worker are running.";
  }

  return (
    <div className="space-y-4">
      <JobSubmitForm />

      {loadError && (
        <p
          role="alert"
          className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-2.5 text-xs text-amber-200"
        >
          {loadError}
        </p>
      )}

      <JobQueueList initialJobs={page.jobs} initialTotal={page.total} />
    </div>
  );
}

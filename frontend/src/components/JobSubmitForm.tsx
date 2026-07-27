"use client";

/** Controlled form; POSTs to /api/v1/jobs and navigates to the run detail view. */

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiError, createJob } from "@/lib/api";

const MAX_LENGTH = 4000;

export function JobSubmitForm() {
  const router = useRouter();
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const trimmed = task.trim();
  const canSubmit = trimmed.length > 0 && !submitting;

  const onSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!canSubmit) return;

    setSubmitting(true);
    setError(null);
    try {
      const created = await createJob(trimmed);
      router.push(`/jobs/${created.job_id}`);
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `Could not submit job (${caught.status}): ${caught.message}`
          : "Could not reach the backend. Is it running?",
      );
      // Only clear the submitting flag on failure — on success we are
      // navigating away, and re-enabling the button invites a double submit.
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={onSubmit}
      className="rounded-lg border border-slate-800 bg-slate-950 p-4"
    >
      <label htmlFor="task" className="block text-sm font-semibold text-slate-200">
        New pipeline job
      </label>
      <p className="mt-1 text-xs text-slate-500">
        Describe a task. The Supervisor decomposes it, Workers execute each
        subtask, and the Verifier approves or sends it back.
      </p>

      <textarea
        id="task"
        name="task_description"
        value={task}
        onChange={(event) => setTask(event.target.value)}
        maxLength={MAX_LENGTH}
        rows={3}
        placeholder="Write a 500-word blog post about IoT water leak detection"
        className="mt-3 w-full resize-y rounded border border-slate-700 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-600 focus:border-blue-500 focus:outline-none"
      />

      <div className="mt-3 flex items-center justify-between gap-3">
        <span className="text-xs text-slate-600">
          {trimmed.length} / {MAX_LENGTH}
        </span>
        <button
          type="submit"
          disabled={!canSubmit}
          className="rounded bg-blue-600 px-4 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-500"
        >
          {submitting ? "Submitting…" : "Run pipeline"}
        </button>
      </div>

      {error && (
        <p role="alert" className="mt-3 text-xs text-red-300">
          {error}
        </p>
      )}
    </form>
  );
}

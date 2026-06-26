/**
 * Jobs persistence (server-backed).
 *
 * A *job* is a named, saved flow chart plus the English narrative rendered from
 * it. The Jobs page lists jobs; opening one launches the flow builder on its
 * saved {@link FlowGraph}, and saving in the builder writes the graph + a freshly
 * rendered narrative back to the job. Persistence is the `/v1/jobs` API (see
 * {@link jobsApi}); this module wraps it with a small in-memory cache so the
 * React hooks can read synchronously and re-render on change.
 *
 * History: this was browser-localStorage-only in the initial flows UI prototype.
 * It now talks to the backend so jobs (and their runs) are shared and runnable.
 */

import { useCallback, useEffect, useState } from "react";
import { type FlowGraph, generateFlowText } from "@/lib/flowToText";
import {
  apiCreateJob,
  apiDeleteJob,
  apiGetJob,
  apiListJobs,
  apiRunJob,
  apiUpdateJob,
  type Job,
  type Run,
} from "@/lib/jobsApi";

export type { Job, Run } from "@/lib/jobsApi";

/** Cross-component change signal so hooks re-read after a mutation. */
const EVENT = "omnigent-jobs-changed";

/** In-memory cache so `useJobs`/`useJob` can read synchronously. */
const jobCache = new Map<string, Job>();
let listLoaded = false;

function emptyGraph(): FlowGraph {
  return { nodes: [], edges: [], loops: [] };
}

function emit(): void {
  window.dispatchEvent(new Event(EVENT));
}

function cacheJob(job: Job): Job {
  jobCache.set(job.id, job);
  return job;
}

/** Newest-updated first. */
function cachedList(): Job[] {
  return [...jobCache.values()].sort((a, b) => b.updatedAt - a.updatedAt);
}

async function refreshList(): Promise<void> {
  const jobs = await apiListJobs();
  jobCache.clear();
  for (const j of jobs) jobCache.set(j.id, j);
  listLoaded = true;
  emit();
}

/** Create a job, rendering its narrative from the graph. Returns the new job. */
export async function createJob(name: string, graph: FlowGraph = emptyGraph()): Promise<Job> {
  const { narrative } = generateFlowText(graph);
  const job = cacheJob(await apiCreateJob({ name: name.trim() || "Untitled flow", graph, narrative }));
  emit();
  return job;
}

/** Patch name and/or graph; a graph change re-renders the narrative. */
export async function updateJob(
  id: string,
  patch: { name?: string; graph?: FlowGraph; agentId?: string | null },
): Promise<void> {
  const input: { name?: string; graph?: FlowGraph; narrative?: string; agentId?: string | null } = {
    name: patch.name,
    agentId: patch.agentId,
  };
  if (patch.graph !== undefined) {
    input.graph = patch.graph;
    input.narrative = generateFlowText(patch.graph).narrative;
  }
  cacheJob(await apiUpdateJob(id, input));
  emit();
}

export async function deleteJob(id: string): Promise<void> {
  await apiDeleteJob(id);
  jobCache.delete(id);
  emit();
}

/** Trigger a run of a job; returns the created {@link Run}. */
export async function runJob(id: string): Promise<Run> {
  return apiRunJob(id);
}

/**
 * Reactive job list — loads from the API on first use and re-reads on any
 * create/update/delete (via the `omnigent-jobs-changed` event).
 */
export function useJobs(): Job[] {
  const [jobs, setJobs] = useState<Job[]>(() => cachedList());
  const refresh = useCallback(() => setJobs(cachedList()), []);
  useEffect(() => {
    window.addEventListener(EVENT, refresh);
    // Always refetch on mount so the list reflects server state.
    void refreshList();
    return () => window.removeEventListener(EVENT, refresh);
  }, [refresh]);
  return jobs;
}

/**
 * Reactive single job. Returns the cached job (if any) plus a `loading` flag so
 * a consumer can tell "still fetching" apart from "truly missing" (404) — the
 * fetch resolves after mount. Fetches from the API unless the cache is already
 * populated by a list load.
 */
export function useJob(id: string | undefined): { job: Job | undefined; loading: boolean } {
  const [job, setJob] = useState<Job | undefined>(() => (id ? jobCache.get(id) : undefined));
  const [loading, setLoading] = useState<boolean>(() => !!id && !jobCache.has(id));
  const refresh = useCallback(() => setJob(id ? jobCache.get(id) : undefined), [id]);
  useEffect(() => {
    if (!id) {
      setLoading(false);
      return;
    }
    window.addEventListener(EVENT, refresh);
    if (jobCache.has(id) && listLoaded) {
      setLoading(false);
    } else {
      setLoading(true);
      apiGetJob(id)
        .then((j) => {
          cacheJob(j);
          emit();
        })
        .catch(() => {
          // 404 or transient — leave the job undefined; the page shows a
          // not-found state once loading clears.
        })
        .finally(() => setLoading(false));
    }
    return () => window.removeEventListener(EVENT, refresh);
  }, [id, refresh]);
  return { job, loading };
}

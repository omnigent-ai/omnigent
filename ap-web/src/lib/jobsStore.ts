/**
 * Jobs persistence (server-backed).
 *
 * A *job* is a named, saved flow chart plus the English narrative rendered from
 * it. The builder edits a {@link FlowStep} tree; on save the tree is persisted
 * as the job's opaque graph JSON and its narrative is rendered (via
 * {@link treeToGraph} → {@link generateFlowText}) and stored alongside, so a run
 * — which feeds that narrative to an agent — replays the saved text.
 *
 * Persistence is the `/v1/jobs` API (see {@link jobsApi}); this module wraps it
 * with a small in-memory cache so the React hooks can read synchronously and
 * re-render on change. A *run* IS an agent session created from the narrative;
 * the backend's run records are adapted here into the {@link Run} shape the
 * Runs view consumes.
 *
 * History: this was browser-localStorage-only in the initial flows UI prototype.
 * It now talks to the backend so jobs and their runs are shared and real.
 */

import { useCallback, useEffect, useState } from "react";
import { generateFlowText } from "@/lib/flowToText";
import { newTree, treeToGraph, type FlowStep } from "@/lib/flowTree";
import {
  apiCreateJob,
  apiDeleteJob,
  apiGetJob,
  apiListJobs,
  apiListRuns,
  apiRunJob,
  apiUpdateJob,
  type Job as ApiJob,
  type Run as ApiRun,
} from "@/lib/jobsApi";

export type RunStatus = "running" | "succeeded" | "failed";

/**
 * One execution of a job's flow — backed by a real agent session. The backend
 * records a run when the job is run; this is its UI projection. `sessionId`
 * links to the session the run opened (`/c/<id>`).
 */
export interface Run {
  id: string;
  /** 1-based, per-job, monotonically increasing (derived from start order). */
  number: number;
  status: RunStatus;
  /** Epoch ms. */
  startedAt: number;
  /** Epoch ms; undefined while still running. */
  finishedAt?: number;
  /** Human-readable progress/log lines surfaced in the Runs view. */
  logs: string[];
  /** The agent session this run created, for deep-linking. */
  sessionId?: string;
  /** How the run was triggered: manual "Run now" vs the time scheduler. */
  trigger: "adhoc" | "scheduled";
}

/** A job's time-trigger schedule (the builder's model). */
export interface Schedule {
  enabled: boolean;
  intervalMinutes: number;
}

export interface Job {
  id: string;
  name: string;
  /** Epoch ms. */
  createdAt: number;
  updatedAt: number;
  /** The flow this job runs, as a top-down step tree (the builder's model). */
  tree: FlowStep;
  /** Execution history, newest last (views sort). */
  runs: Run[];
  /** Agent the job runs as, or null until one is picked. */
  agentId: string | null;
  /** Time-trigger schedule, or null if the job is unscheduled. */
  schedule: Schedule | null;
  /** Preferred host for runs, or null to pick any online host. */
  hostId: string | null;
}

/** Cross-component change signal so the hooks re-read after a mutation. */
const EVENT = "omnigent-jobs-changed";

/** In-memory cache so `useJobs`/`useJob` can read synchronously. */
const jobCache = new Map<string, Job>();
let listLoaded = false;

function emit(): void {
  window.dispatchEvent(new Event(EVENT));
}

/** Parse the opaque graph JSON the backend round-trips back into a tree. */
function treeFromApi(job: ApiJob): FlowStep {
  const g = job.graph as unknown;
  // The graph field stores the builder's tree verbatim; fall back to a fresh
  // Start-only tree for anything unexpected (older/blank jobs) rather than crash.
  if (g && typeof g === "object" && "type" in (g as Record<string, unknown>)) {
    return g as FlowStep;
  }
  return newTree();
}

/** Map a backend run into the UI {@link Run} shape. `number` is 1-based. */
function runFromApi(r: ApiRun, number: number): Run {
  const status: RunStatus =
    r.status === "finished" ? "succeeded" : r.status === "failed" ? "failed" : "running";
  const logs = [`Run #${number} started`];
  if (r.sessionId) logs.push(`Session ${r.sessionId}`);
  if (status === "succeeded") logs.push("Run finished — open the session to see results.");
  if (status === "failed") logs.push(r.error ? `Run failed: ${r.error}` : "Run failed.");
  return {
    id: r.id,
    number,
    status,
    startedAt: r.startedAt,
    finishedAt: r.completedAt ?? undefined,
    logs,
    sessionId: r.sessionId ?? undefined,
    trigger: r.trigger,
  };
}

/** Assemble the app-facing {@link Job} from a backend job + its runs. */
function toJob(api: ApiJob, runs: ApiRun[]): Job {
  // Backend lists runs newest-first; number them 1..n in start order.
  const ascending = [...runs].sort((a, b) => a.startedAt - b.startedAt);
  const numberById = new Map(ascending.map((r, i) => [r.id, i + 1]));
  return {
    id: api.id,
    name: api.name,
    createdAt: api.createdAt,
    updatedAt: api.updatedAt,
    tree: treeFromApi(api),
    agentId: api.agentId,
    schedule: api.scheduleConfig
      ? {
          enabled: api.scheduleConfig.enabled,
          intervalMinutes: api.scheduleConfig.intervalMinutes,
        }
      : null,
    hostId: api.hostId,
    runs: runs.map((r) => runFromApi(r, numberById.get(r.id) ?? 0)),
  };
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
  // The list view shows run badges, so each job needs its runs. They're cheap
  // (small lists) and the fetches run concurrently.
  await Promise.all(
    jobs.map(async (j) => {
      const runs = await apiListRuns(j.id).catch(() => []);
      cacheJob(toJob(j, runs));
    }),
  );
  listLoaded = true;
  emit();
}

async function refreshJob(id: string): Promise<Job | undefined> {
  const [api, runs] = await Promise.all([apiGetJob(id), apiListRuns(id).catch(() => [])]);
  return cacheJob(toJob(api, runs));
}

/** Render the narrative the backend persists for a tree. */
function narrativeFor(tree: FlowStep): string {
  return generateFlowText(treeToGraph(tree)).narrative;
}

/** Create a job from a tree, rendering its narrative. Returns the new job. */
export async function createJob(name: string, tree: FlowStep = newTree()): Promise<Job> {
  const api = await apiCreateJob({
    name: name.trim() || "Untitled flow",
    graph: tree,
    narrative: narrativeFor(tree),
  });
  const job = cacheJob(toJob(api, []));
  emit();
  return job;
}

/** Patch name, tree, agent, schedule, and/or host; a tree change re-renders the narrative. */
export async function updateJob(
  id: string,
  patch: {
    name?: string;
    tree?: FlowStep;
    agentId?: string | null;
    schedule?: Schedule | null;
    hostId?: string | null;
  },
): Promise<void> {
  const input: {
    name?: string;
    graph?: FlowStep;
    narrative?: string;
    agentId?: string | null;
    scheduleConfig?: { enabled: boolean; intervalMinutes: number } | null;
    hostId?: string | null;
  } = { name: patch.name, agentId: patch.agentId };
  if (patch.tree !== undefined) {
    input.graph = patch.tree;
    input.narrative = narrativeFor(patch.tree);
  }
  if (patch.schedule !== undefined) {
    input.scheduleConfig = patch.schedule;
  }
  if (patch.hostId !== undefined) {
    input.hostId = patch.hostId;
  }
  await apiUpdateJob(id, input);
  await refreshJob(id);
  emit();
}

export async function deleteJob(id: string): Promise<void> {
  await apiDeleteJob(id);
  jobCache.delete(id);
  emit();
}

/** The most recent run for a job (by start time), or undefined if never run. */
export function latestRun(job: Job): Run | undefined {
  if (!job.runs.length) return undefined;
  return job.runs.reduce((a, b) => (b.startedAt > a.startedAt ? b : a));
}

/**
 * Run a job now: the backend renders the saved narrative, creates an agent
 * session seeded with it, and records a run. The job's runs are refreshed so
 * the Runs view reflects the new run; the created {@link Run} is returned.
 */
export async function runJob(id: string): Promise<Run | undefined> {
  await apiRunJob(id);
  const job = await refreshJob(id);
  emit();
  return job ? latestRun(job) : undefined;
}

/**
 * Reactive job list — loads from the API on first use and re-reads on any
 * create/update/delete/run (via the `omnigent-jobs-changed` event).
 */
export function useJobs(): Job[] {
  const [jobs, setJobs] = useState<Job[]>(() => cachedList());
  const refresh = useCallback(() => setJobs(cachedList()), []);
  useEffect(() => {
    window.addEventListener(EVENT, refresh);
    void refreshList();
    return () => window.removeEventListener(EVENT, refresh);
  }, [refresh]);
  return jobs;
}

/**
 * Reactive single job. Returns the cached job plus a `loading` flag so a
 * consumer can tell "still fetching" apart from "truly missing" (404) — the
 * fetch resolves after mount. Re-reads on any store change so the Runs tab
 * reflects run progress.
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
      refreshJob(id)
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

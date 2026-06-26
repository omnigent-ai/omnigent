/**
 * Jobs persistence (browser localStorage).
 *
 * A *job* is a named, saved flow chart. The Jobs page lists jobs; opening one
 * launches the flow builder on its saved {@link FlowGraph}, and saving in the
 * builder writes back to the same job. Storage is intentionally client-side
 * only (one `localStorage` key) — no server/API involvement — mirroring the
 * original standalone prototype's persistence model.
 *
 * The module is framework-agnostic (plain functions over `localStorage`); the
 * React layer wraps it with {@link useJobs} for reactive reads.
 */

import { useCallback, useEffect, useState } from "react";
import type { FlowGraph } from "@/lib/flowToText";

const STORAGE_KEY = "omnigent-jobs-v1";

export interface Job {
  id: string;
  name: string;
  /** Epoch ms. */
  createdAt: number;
  updatedAt: number;
  /** The saved flow chart this job was created from. */
  graph: FlowGraph;
}

/** Cross-tab + same-tab change signal so `useJobs` can re-read. */
const EVENT = "omnigent-jobs-changed";

function emptyGraph(): FlowGraph {
  return { nodes: [], edges: [], loops: [] };
}

function readAll(): Job[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // Tolerate partially-shaped entries from older writes.
    return parsed
      .filter((j): j is Job => j && typeof j.id === "string" && typeof j.name === "string")
      .map((j) => ({ ...j, graph: j.graph ?? emptyGraph() }));
  } catch {
    return [];
  }
}

function writeAll(jobs: Job[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(jobs));
  } catch {
    // Quota / disabled storage — nothing actionable; the in-memory list still
    // reflects the change for this session.
  }
  // Notify same-tab listeners (the native `storage` event only fires in OTHER
  // tabs). `CustomEvent` is fine in every browser the app targets.
  window.dispatchEvent(new Event(EVENT));
}

const uid = () =>
  `job_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;

export function listJobs(): Job[] {
  // Newest first.
  return readAll().sort((a, b) => b.updatedAt - a.updatedAt);
}

export function getJob(id: string): Job | undefined {
  return readAll().find((j) => j.id === id);
}

export function createJob(name: string, graph: FlowGraph = emptyGraph()): Job {
  const now = Date.now();
  const job: Job = { id: uid(), name: name.trim() || "Untitled flow", createdAt: now, updatedAt: now, graph };
  writeAll([...readAll(), job]);
  return job;
}

/** Patch name and/or graph; bumps `updatedAt`. No-op if the id is unknown. */
export function updateJob(id: string, patch: Partial<Pick<Job, "name" | "graph">>): void {
  const jobs = readAll();
  const i = jobs.findIndex((j) => j.id === id);
  if (i === -1) return;
  jobs[i] = { ...jobs[i], ...patch, updatedAt: Date.now() };
  writeAll(jobs);
}

export function deleteJob(id: string): void {
  writeAll(readAll().filter((j) => j.id !== id));
}

/**
 * Reactive job list — re-reads on any create/update/delete (this tab) and on
 * `localStorage` changes from other tabs.
 */
export function useJobs(): Job[] {
  const [jobs, setJobs] = useState<Job[]>(() => listJobs());
  const refresh = useCallback(() => setJobs(listJobs()), []);
  useEffect(() => {
    window.addEventListener(EVENT, refresh);
    window.addEventListener("storage", refresh);
    return () => {
      window.removeEventListener(EVENT, refresh);
      window.removeEventListener("storage", refresh);
    };
  }, [refresh]);
  return jobs;
}

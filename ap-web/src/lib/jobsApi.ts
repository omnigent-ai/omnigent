/**
 * Typed client for the `/v1/jobs` and `/v1/runs` endpoints.
 *
 * A *job* is a saved AI workflow (a node graph + the English narrative rendered
 * from it). A *run* is one execution of a job — an agent session created from
 * the narrative. Mirrors `omnigent/server/routes/jobs.py`.
 *
 * Naming: the TS surface is camelCase; the wire is snake_case. Timestamps are
 * epoch **seconds** on the wire and converted to epoch **ms** here so the rest
 * of the app (which formats with `relativeTime`) sees a uniform ms value.
 *
 * All requests go through the existing Vite `/v1` proxy, so no proxy changes
 * are needed.
 */

import { authenticatedFetch } from "./identity";
import { ApiError } from "./sessionsApi";

/**
 * The flow payload is opaque to this layer and to the backend — it round-trips
 * as JSON. The store decides its concrete shape (currently a `FlowStep` tree).
 */
export type FlowPayload = unknown;

/** A saved job, camelCased with ms timestamps. */
export interface Job {
  id: string;
  name: string;
  /** Epoch ms. */
  createdAt: number;
  updatedAt: number;
  graph: FlowPayload;
  narrative: string;
  agentId: string | null;
}

/** One execution of a job. */
export interface Run {
  id: string;
  jobId: string;
  sessionId: string | null;
  status: "running" | "finished" | "failed";
  /** Epoch ms. */
  startedAt: number;
  completedAt: number | null;
  error: string | null;
}

interface JobWire {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  graph: FlowPayload;
  narrative: string;
  agent_id: string | null;
}

interface RunWire {
  id: string;
  job_id: string;
  session_id: string | null;
  status: "running" | "finished" | "failed";
  started_at: number;
  completed_at: number | null;
  error: string | null;
}

function jobFromWire(w: JobWire): Job {
  return {
    id: w.id,
    name: w.name,
    createdAt: w.created_at * 1000,
    updatedAt: w.updated_at * 1000,
    graph: w.graph ?? null,
    narrative: w.narrative ?? "",
    agentId: w.agent_id ?? null,
  };
}

function runFromWire(w: RunWire): Run {
  return {
    id: w.id,
    jobId: w.job_id,
    sessionId: w.session_id,
    status: w.status,
    startedAt: w.started_at * 1000,
    completedAt: w.completed_at == null ? null : w.completed_at * 1000,
    error: w.error,
  };
}

async function readJsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    let code: string | null = null;
    try {
      const body = (await res.json()) as { error?: { code?: string; message?: string } };
      if (body.error?.message) message = body.error.message;
      if (body.error?.code) code = body.error.code;
    } catch {
      // Non-JSON / empty body — keep the status-line fallback.
    }
    throw new ApiError(message, res.status, code);
  }
  return (await res.json()) as T;
}

/** Payload accepted by {@link apiCreateJob} / {@link apiUpdateJob}. */
export interface JobInput {
  name?: string;
  graph?: FlowPayload;
  narrative?: string;
  agentId?: string | null;
}

function toBody(input: JobInput): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.name !== undefined) body.name = input.name;
  if (input.graph !== undefined) body.graph = input.graph;
  if (input.narrative !== undefined) body.narrative = input.narrative;
  if (input.agentId !== undefined) body.agent_id = input.agentId;
  return body;
}

export async function apiListJobs(): Promise<Job[]> {
  const res = await authenticatedFetch("/v1/jobs");
  const wires = await readJsonOrThrow<JobWire[]>(res);
  return wires.map(jobFromWire);
}

export async function apiGetJob(id: string): Promise<Job> {
  const res = await authenticatedFetch(`/v1/jobs/${encodeURIComponent(id)}`);
  return jobFromWire(await readJsonOrThrow<JobWire>(res));
}

export async function apiCreateJob(input: JobInput): Promise<Job> {
  const res = await authenticatedFetch("/v1/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toBody(input)),
  });
  return jobFromWire(await readJsonOrThrow<JobWire>(res));
}

export async function apiUpdateJob(id: string, input: JobInput): Promise<Job> {
  const res = await authenticatedFetch(`/v1/jobs/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toBody(input)),
  });
  return jobFromWire(await readJsonOrThrow<JobWire>(res));
}

export async function apiDeleteJob(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/jobs/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok && res.status !== 404) throw await apiErrorOf(res);
}

export async function apiRunJob(id: string): Promise<Run> {
  const res = await authenticatedFetch(`/v1/jobs/${encodeURIComponent(id)}/run`, {
    method: "POST",
  });
  return runFromWire(await readJsonOrThrow<RunWire>(res));
}

export async function apiListRuns(jobId: string, status?: string): Promise<Run[]> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  const res = await authenticatedFetch(`/v1/jobs/${encodeURIComponent(jobId)}/runs${qs}`);
  const wires = await readJsonOrThrow<RunWire[]>(res);
  return wires.map(runFromWire);
}

async function apiErrorOf(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  try {
    const body = (await res.json()) as { error?: { message?: string } };
    if (body.error?.message) message = body.error.message;
  } catch {
    // keep fallback
  }
  return new ApiError(message, res.status, null);
}

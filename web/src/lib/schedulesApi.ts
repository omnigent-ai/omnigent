/**
 * Typed client for the `/v1/schedules` endpoints.
 * Mirrors `omnigent/server/routes/schedules.py`.
 */

import { authenticatedFetch } from "./identity";

/** A loop (cron) or monitor (stream) schedule. */
export interface Schedule {
  id: string;
  object: "schedule";
  /** Conversation this fires into, or null for a global loop. */
  conversation_id: string | null;
  /** Registered agent a global loop spawns a fresh run for, or null. */
  agent_name: string | null;
  name: string;
  kind: "loop" | "monitor";
  prompt: string;
  cron: string | null;
  command: string | null;
  enabled: boolean;
  status: string;
  last_fired_at: number | null;
  last_run_id: string | null;
  created_at: number;
  updated_at: number | null;
}

export async function listSchedules(conversationId: string): Promise<Schedule[]> {
  const res = await authenticatedFetch(
    `/v1/schedules?conversation_id=${encodeURIComponent(conversationId)}`,
  );
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { object: string; data: Schedule[] };
  return body.data;
}

/** List every schedule across the workspace (global + conversation-scoped). */
export async function listAllSchedules(): Promise<Schedule[]> {
  const res = await authenticatedFetch(`/v1/schedules`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { object: string; data: Schedule[] };
  return body.data;
}

/** A runner (host) currently connected to the server and able to run turns. */
export interface OnlineRunner {
  runner_id: string;
  online: boolean;
  harnesses: string[];
}

/**
 * List runners currently connected (scoped to the caller). An empty list means
 * no host is online — global loops can't spawn a fresh run until one connects.
 */
export async function listOnlineRunners(): Promise<OnlineRunner[]> {
  const res = await authenticatedFetch(`/v1/runners`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  const body = (await res.json()) as { data: OnlineRunner[] };
  return body.data;
}

export interface UpdateSchedulePatch {
  name?: string;
  prompt?: string;
  cron?: string;
  command?: string;
  enabled?: boolean;
}

export async function updateSchedule(id: string, patch: UpdateSchedulePatch): Promise<Schedule> {
  const res = await authenticatedFetch(`/v1/schedules/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return (await res.json()) as Schedule;
}

export async function deleteSchedule(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/schedules/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
}

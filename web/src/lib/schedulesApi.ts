/**
 * Typed client for the `/v1/schedules` endpoints.
 * Mirrors `omnigent/server/routes/schedules.py`.
 */

import { authenticatedFetch } from "./identity";

/** A loop (cron) or monitor (stream) schedule. */
export interface Schedule {
  id: string;
  object: "schedule";
  conversation_id: string;
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

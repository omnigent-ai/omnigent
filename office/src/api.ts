// Browser client for the office service. It never talks to Omnigent directly
// and never sees Omnigent credentials.

import type { BuildingSnapshot, RoomSettingsPatch } from "../shared/types.ts";

export interface WorkerSession {
  id: string;
  title: string | null;
  agent_name: string | null;
  status: string;
  project_id: string | null;
  created_at: number;
  updated_at: number;
  host_online: boolean | null;
  runner_online: boolean | null;
  pending_elicitations_count: number;
}

export interface PendingElicitation {
  elicitation_id: string;
  params?: { message?: string; requestedSchema?: unknown; [key: string]: unknown };
  [key: string]: unknown;
}

export interface SessionDetail {
  id: string;
  title: string | null;
  status: string;
  harness?: string | null;
  agent_name?: string | null;
  llm_model?: string | null;
  host_online?: boolean | null;
  pending_elicitations?: PendingElicitation[];
  todos?: { content: string; status: string; activeForm?: string }[] | null;
  last_task_error?: { message?: string } | null;
}

export interface SessionItem {
  id: string;
  type: string;
  role?: string;
  created_at?: number;
  content?: { type: string; text?: string }[];
  name?: string;
  arguments?: string;
  message?: string;
  is_meta?: boolean;
}

export class OfficeApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function call<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const response = await fetch(`/office/api${path}`, {
    method: init?.method ?? "GET",
    headers: init?.body === undefined ? undefined : { "Content-Type": "application/json" },
    body: init?.body === undefined ? undefined : JSON.stringify(init.body),
  });
  const body = (await response.json().catch(() => null)) as
    | (T & { error?: { code: string; message: string } })
    | null;
  if (!response.ok) {
    throw new OfficeApiError(
      response.status,
      body?.error?.code ?? "error",
      body?.error?.message ?? `${response.status} ${response.statusText}`,
    );
  }
  return body as T;
}

export const officeApi = {
  building: () => call<BuildingSnapshot>("/building"),
  refreshBuilding: () => call<BuildingSnapshot>("/building/refresh", { method: "POST" }),
  updateRoom: (projectId: string, patch: RoomSettingsPatch) =>
    call<BuildingSnapshot>(`/rooms/${encodeURIComponent(projectId)}`, { method: "PATCH", body: patch }),
  workers: async (projectId: string) =>
    (await call<{ data: WorkerSession[] }>(`/rooms/${encodeURIComponent(projectId)}/workers`)).data,
  session: (sessionId: string) => call<SessionDetail>(`/sessions/${encodeURIComponent(sessionId)}`),
  items: async (sessionId: string) =>
    (await call<{ data: SessionItem[] }>(`/sessions/${encodeURIComponent(sessionId)}/items`)).data,
  sendMessage: (sessionId: string, text: string) =>
    call<{ queued: boolean; denied?: boolean }>(`/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      body: { text },
    }),
  stop: (sessionId: string) => call(`/sessions/${encodeURIComponent(sessionId)}/stop`, { method: "POST" }),
  decide: (sessionId: string, elicitationId: string, action: "accept" | "decline", feedback?: string) =>
    call<{ replayed: boolean }>(
      `/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}/decision`,
      { method: "POST", body: { action, feedback } },
    ),
};

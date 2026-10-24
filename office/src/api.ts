// Browser client for the office service. It never talks to Omnigent directly
// and never sees Omnigent credentials.

import type { GateType } from "../shared/sdlcTheme.ts";
import type { BuildingSnapshot, RoomSettingsPatch } from "../shared/types.ts";
import type { BranchRuntime, BranchView, Requirement } from "../shared/workflowTypes.ts";

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
  labels?: Record<string, string>;
}

export interface HarnessOption {
  id: string;
  label?: string;
  capabilities?: Record<string, string | boolean | null>;
}

export interface HostOption {
  host_id: string;
  name?: string;
  status?: string;
  sandbox_provider?: string | null;
  configured_harnesses?: string[] | null;
}

export interface RuntimeOptions {
  hosts: { hosts?: HostOption[] };
  harnesses: { data?: HarnessOption[] };
  dictationAvailable: boolean;
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
  office_relayed?: boolean;
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

export interface NewRoomRequest {
  name: string;
  icon: string;
  hostId?: string;
  workspace?: string;
  /** SDLC runtime and gates; host and folder come from the room itself. */
  theme?: { runtime: Omit<BranchRuntime, "hostId" | "workspace">; gates: Partial<Record<GateType, boolean>> } | null;
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
  createRoom: (room: NewRoomRequest) => call<{ projectId: string; snapshot: BuildingSnapshot }>("/rooms", { method: "POST", body: room }),
  hostDirectories: (hostId: string, path: string) =>
    call<{ directories: { name: string; path: string }[]; hasMore: boolean }>(
      `/hosts/${encodeURIComponent(hostId)}/directories?path=${encodeURIComponent(path)}`,
    ),
  runtimeOptions: () => call<RuntimeOptions>("/runtime-options"),
  branch: async (projectId: string) =>
    (await call<{ branch: BranchView | null }>(`/rooms/${encodeURIComponent(projectId)}/branch`)).branch,
  assignTheme: (projectId: string, runtime: BranchRuntime, gates: Partial<Record<GateType, boolean>>) =>
    call<BranchView>(`/rooms/${encodeURIComponent(projectId)}/theme`, { method: "POST", body: { runtime, gates } }),
  updateBoard: (branchId: string, baseRevision: number, requirements: Requirement[]) =>
    call<BranchView>(`/branches/${branchId}/board`, { method: "PUT", body: { baseRevision, requirements } }),
  decideProposal: (branchId: string, proposalId: string, action: "accept" | "reject", feedback: string) =>
    call<BranchView>(`/branches/${branchId}/proposals/${proposalId}/decision`, { method: "POST", body: { action, feedback } }),
  decideGate: (branchId: string, gateId: string, action: "approve" | "request_changes", digest: string, feedback: string) =>
    call<{ view: BranchView; replayed: boolean }>(`/branches/${branchId}/gates/${gateId}/decision`, {
      method: "POST",
      body: { action, digest, feedback },
    }),
  startInterview: (branchId: string, topic: string) =>
    call<BranchView>(`/branches/${branchId}/interview`, { method: "POST", body: { topic } }),
  answerInterview: (branchId: string, questionId: string, answer: string) =>
    call<BranchView>(`/branches/${branchId}/interviews/${questionId}/answer`, { method: "POST", body: { answer } }),
  completeTask: (branchId: string, taskId: string, summary: string) =>
    call<BranchView>(`/branches/${branchId}/tasks/${taskId}/complete`, { method: "POST", body: { summary } }),
  stopBranch: (branchId: string) => call<{ stopped: number; failed: string[] }>(`/branches/${branchId}/stop`, { method: "POST", body: {} }),
  rollover: (branchId: string, role: string, reason: string) =>
    call<BranchView>(`/branches/${branchId}/roles/${role}/rollover`, { method: "POST", body: { reason } }),
};

// Browser client for the office service. It never talks to Omnigent directly
// and never sees Omnigent credentials.

import type { HarnessModels } from "../shared/harnessModels.ts";
import type { ProjectLink } from "../shared/projectLinks.ts";
import type { TeamDefinition } from "../shared/team.ts";
import type { RoleToolReport, RoleToolSettings } from "../shared/roleTools.ts";
import type { TeamTemplate } from "../shared/teamTemplates.ts";
import type { RoomMessage } from "../shared/roomMessages.ts";
import type { RoleWorkLine } from "../shared/roleWork.ts";
import type { AgentRoleId, GateType } from "../shared/sdlcTheme.ts";
import type { ScheduledRetry } from "../shared/troubleshoot.ts";
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
  /** Harness id to readiness: true, false, "needs-auth", "binary-missing", or "version-too-low". */
  configured_harnesses?: Record<string, unknown> | null;
}

export interface RuntimeOptions {
  hosts: { hosts?: HostOption[] };
  harnesses: { data?: HarnessOption[] };
  dictationAvailable: boolean;
  /** The connected Omnigent's address, for opening a session in its own UI. */
  omnigentUrl?: string;
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

export interface WorkspaceEntry {
  name: string;
  /** Relative to the room's folder. */
  path: string;
  type: "directory" | "file";
  bytes: number | null;
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
  harnessModels: (hostId: string, harness: string) =>
    call<HarnessModels>(`/hosts/${encodeURIComponent(hostId)}/harnesses/${encodeURIComponent(harness)}/models`),
  roomMessages: async (projectId: string) => (await call<{ data: RoomMessage[] }>(`/rooms/${encodeURIComponent(projectId)}/messages`)).data,
  sendRoomMessage: (projectId: string, text: string, recipients: { sessionId: string; name: string }[]) =>
    call<{ message: RoomMessage | null; failures: { name: string; error: string }[] }>(`/rooms/${encodeURIComponent(projectId)}/messages`, {
      method: "POST",
      body: { text, recipients },
    }),
  roomLinks: (projectId: string) =>
    call<{ links: ProjectLink[]; app: { url: string; folder: string } | null }>(`/rooms/${encodeURIComponent(projectId)}/links`),
  addRoomLink: (projectId: string, label: string, url: string) =>
    call<{ link: ProjectLink }>(`/rooms/${encodeURIComponent(projectId)}/links`, { method: "POST", body: { label, url } }),
  removeRoomLink: (projectId: string, linkId: string) =>
    call<{ removed: boolean }>(`/rooms/${encodeURIComponent(projectId)}/links/${encodeURIComponent(linkId)}`, { method: "DELETE" }),
  roomUsage: (projectId: string) => call<{ totalTokens: number; costUsd: number; sessions: number }>(`/rooms/${encodeURIComponent(projectId)}/usage`),
  roomFiles: (projectId: string, path: string) =>
    call<{ workspace: string; path: string; entries: WorkspaceEntry[]; hasMore: boolean }>(
      `/rooms/${encodeURIComponent(projectId)}/files?path=${encodeURIComponent(path)}`,
    ),
  roomFile: (projectId: string, path: string) =>
    call<{ path: string; content: string | null; truncated: boolean; bytes: number | null; contentType: string | null }>(
      `/rooms/${encodeURIComponent(projectId)}/files/content?path=${encodeURIComponent(path)}`,
    ),
  closeTemporaryWorkers: (branchId: string, sessionIds: string[]) =>
    call<{ closed: string[]; failed: string[] }>(`/branches/${branchId}/temporary-workers/close`, { method: "POST", body: { sessionIds } }),
  closeReplacedSessions: (branchId: string, sessionIds: string[]) =>
    call<{ closed: string[]; failed: string[] }>(`/branches/${branchId}/replaced-sessions/close`, { method: "POST", body: { sessionIds } }),
  teamConfig: async (branchId: string) =>
    (await call<{ data: { role: AgentRoleId; prompt: string; customInstructions: string }[] }>(`/branches/${branchId}/team-config`)).data,
  setTeam: (branchId: string, team: TeamDefinition) => call<BranchView>(`/branches/${branchId}/team`, { method: "PUT", body: { team } }),
  officeTools: async () => (await call<{ data: { name: string; description: string; coordinatorOnly: boolean }[] }>("/office-tools")).data,
  teamTemplates: async () => (await call<{ data: TeamTemplate[] }>("/team-templates")).data,
  saveTeamTemplate: (branchId: string, name: string, description: string) =>
    call<{ template: TeamTemplate }>(`/branches/${branchId}/team-templates`, { method: "POST", body: { name, description } }),
  applyTeamTemplate: (branchId: string, templateId: string, restart: boolean) =>
    call<{ view: BranchView; changed: AgentRoleId[] }>(`/branches/${branchId}/team-templates/${encodeURIComponent(templateId)}/apply`, {
      method: "POST",
      body: { restart },
    }),
  importTeamTemplate: (yaml: string) => call<{ template: TeamTemplate; replaced: boolean }>("/team-templates/import", { method: "POST", body: { yaml } }),
  deleteTeamTemplate: (templateId: string) => call<{ deleted: boolean }>(`/team-templates/${encodeURIComponent(templateId)}`, { method: "DELETE" }),
  setRoleToolSettings: (branchId: string, role: AgentRoleId, settings: RoleToolSettings, restart: boolean) =>
    call<BranchView>(`/branches/${branchId}/roles/${role}/tool-settings`, { method: "PUT", body: { settings, restart } }),
  setRoleInstructions: (branchId: string, role: AgentRoleId, text: string, restart: boolean) =>
    call<BranchView>(`/branches/${branchId}/roles/${role}/instructions`, { method: "PUT", body: { text, restart } }),
  roleWork: async (branchId: string) => (await call<{ data: RoleWorkLine[] }>(`/branches/${branchId}/role-work`)).data,
  roleTools: async (branchId: string) => (await call<{ data: RoleToolReport[] }>(`/branches/${branchId}/tools`)).data,
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
  recoverSession: (branchId: string, sessionId: string, action: "continue" | "recover" | "nudge" | "stop") =>
    call<{ result: string }>(`/branches/${branchId}/sessions/${encodeURIComponent(sessionId)}/recover`, { method: "POST", body: { action } }),
  scheduleRetry: (branchId: string, sessionId: string, minutes: number | null, repeat: boolean) =>
    call<{ retry: ScheduledRetry | null }>(`/branches/${branchId}/sessions/${encodeURIComponent(sessionId)}/retry-schedule`, {
      method: "POST",
      body: { minutes, repeat },
    }),
  dismissProblem: (branchId: string, subject: string, key: string) =>
    call<{ dismissed: boolean }>(`/branches/${branchId}/problems/dismiss`, { method: "POST", body: { subject, key } }),
  switchHarness: (branchId: string, roles: string[], harness: string, model: string, effort: string, reason: string) =>
    call<BranchView>(`/branches/${branchId}/roles/switch-harness`, { method: "POST", body: { roles, harness, model, effort, reason } }),
};

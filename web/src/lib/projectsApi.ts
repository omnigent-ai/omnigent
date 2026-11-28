// Typed client for the `/v1/projects` first-class projects CRUD
// (`omnigent/server/routes/projects.py`). Projects are owner-private
// containers that group sessions and exist independently of their members —
// so they can be empty, renamed, and deleted without touching sessions.
//
// Session→project membership lives on the session, not here: file/unfile a
// session with `PATCH /v1/sessions/{id}` `{ project_id }` (see sessionsApi).
//
// All requests go through the existing Vite `/v1` proxy. TS surface is
// camelCase-friendly, but the project shape is already flat snake_case-free
// (`id`, `name`), so no boundary conversion is needed.

import { authenticatedFetch } from "./identity";
import { apiErrorFromResponse } from "./sessionsApi";

/**
 * Default session settings a project stores, pre-filled into the new-chat
 * composer. All fields optional — an unset key means "no default for this
 * slot". Creation uses these choices for the Lead; later sessions prefill from them.
 */
export interface ProjectConfig {
  /** People besides the owner whose own `product-ok` approves others' features. Owner-only. */
  product_approvers?: string[];
  /** Projects whose Lead may read this board's tickets, comments and stages. Owner-only. */
  board_readers?: string[];
  /** Default host id, or the sandbox sentinel. */
  host_id?: string;
  /** Default working directory / repo path on that host. */
  workspace?: string;
  /**
   * Repository the project was created with. A connected-host project has it
   * checked out at `workspace`; a sandbox project clones it into each new session.
   */
  repository?: { url: string; branch?: string };
  /** Sandbox provider a sandbox project runs on, e.g. "modal". */
  sandbox_provider?: string;
  /** Default agent id for new sessions. */
  agent_id?: string;
  /** Per-session harness override for a configurable agent. */
  harness?: string;
  /** Chosen emoji icon (a unicode grapheme, e.g. "🔥"). Unset → default folder. */
  icon?: string;
  /**
   * Per-project worktree default. When `true`, a new session in a git workspace
   * starts in a fresh randomly-named worktree; when `false`, it starts directly
   * in the workspace. Both are meaningful: a set value overrides the user-global
   * "always use a worktree" default (Settings › Git). An unset key falls through
   * to that global default.
   */
  use_worktree?: boolean;
  /**
   * Default base branch a new worktree forks from, pre-filled into the
   * composer's base-branch field. Takes precedence over the user-global default
   * (Settings › Git); an unset key falls through to that global default. Blank
   * is never stored (treated the same as unset).
   */
  base_branch?: string;
  /**
   * Default model for new sessions when the default agent is a native coding
   * harness with a model choice (e.g. Claude Code's version-agnostic "opus"
   * alias, or a Codex model id resolved on the host). Only meaningful
   * alongside an `agent_id` whose harness takes a model override; unset =
   * the harness's own configured default.
   */
  model?: string;
  /**
   * Default reasoning effort for new sessions with the default agent (e.g.
   * `"high"`), from that harness's ladder; Codex validates it against the
   * model's ladder. Unset = the harness's own configured effort.
   */
  effort?: string;
}

/** A first-class project. Mirrors the `ProjectObject` response shape. */
export interface Project {
  id: string;
  name: string;
  main_session_id?: string | null;
  main_session_state?: "absent" | "missing" | "archived" | "active" | "unavailable";
  /** @deprecated Read-only compatibility with servers before 0.25.0. */
  orchestrator_session_id?: string | null;
  /** Owner user id; `null` in single-user / OSS mode. */
  user_id?: string | null;
  created_at?: number;
  updated_at?: number | null;
  /** Stored default session settings; `{}` when the project has none. */
  config?: ProjectConfig;
  /** Owner-written guidance loaded into every project session; `null` when unset. */
  mandate?: string | null;
  /** Proposed mandate additions awaiting the owner, oldest first. */
  mandate_proposals?: MandateProposal[];
  /**
   * The project's charter for people (mission, scope, out of scope, how we'll know
   * it's working, starting roster); `null` when unset. Only the owner or Lead edits it.
   */
  description?: string | null;
  /** The owning user; `null` in single-user mode. */
  owner_user_id?: string | null;
  /** True when another user owns the project and shared it with the caller. */
  shared?: boolean;
  /** The owner's chosen router persona (`"orchestrator"` is the Lead); `null` = default. */
  router_identity_id?: string | null;
  /** Whether the owner removed the built-in Lead persona. */
  lead_removed?: boolean;
  /** Who receives ticket events, digests and escalations now, after fallbacks. */
  router?: { identity_id: string; name: string } | null;
}

/** The Lead's identity id; it keeps its original value so stored links stay valid. */
export const LEAD_IDENTITY_ID = "orchestrator";

/** `manager` may also invite and remove ordinary members; only the owner sets it. */
export type ProjectShareRole = "member" | "manager";

/** A user a project is shared with; members read and write everything in it. */
export interface ProjectShare {
  user_id: string;
  created_at: number;
  created_by?: string | null;
  role?: ProjectShareRole;
}

export interface ProjectShareList {
  owner_user_id: string | null;
  /** The caller's own role; absent from older servers. */
  viewer_role?: "owner" | ProjectShareRole | null;
  data: ProjectShare[];
}

function sharesPath(projectId: string, userId?: string): string {
  const base = `/v1/projects/${encodeURIComponent(projectId)}/shares`;
  return userId === undefined ? base : `${base}/${encodeURIComponent(userId)}`;
}

/** List the users a project is shared with. Any member may read it. */
export async function listProjectShares(projectId: string): Promise<ProjectShareList> {
  const res = await authenticatedFetch(sharesPath(projectId));
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectShareList;
}

/** Share a project with another user by their sign-in ID. The owner or a Manager. */
export async function shareProject(
  projectId: string,
  userId: string,
  role: ProjectShareRole = "member",
): Promise<ProjectShare> {
  const res = await authenticatedFetch(sharesPath(projectId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(role === "member" ? { user_id: userId } : { user_id: userId, role }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectShare;
}

/** Make a member a Manager or back. Owner-only. */
export async function setProjectShareRole(
  projectId: string,
  userId: string,
  role: ProjectShareRole,
): Promise<ProjectShare> {
  const res = await authenticatedFetch(sharesPath(projectId, userId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectShare;
}

/** Revoke a user's share. The owner, or a Manager for an ordinary member. */
export async function revokeProjectShare(projectId: string, userId: string): Promise<void> {
  const res = await authenticatedFetch(sharesPath(projectId, userId), { method: "DELETE" });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

/** A proposed addition to a project's mandate; it applies only once the owner accepts it. */
export interface MandateProposal {
  id: string;
  text: string;
  created_at: number;
  /** The proposing session, or `null` when the owner proposed it. */
  session_id?: string | null;
  /** `append` adds `text`; the others replace a section (blank `text` deletes it) or everything. */
  mode?: "append" | "replace_section" | "replace_all";
  /** The heading title a `replace_section` proposal targets. */
  section?: string | null;
  /** The mandate text accepting it would remove; `null` when its section is gone. */
  current_text?: string | null;
  /** The text it replaces changed since it was proposed, so it can't be accepted. */
  stale?: boolean;
}

/** Most UTF-8 bytes a mandate may hold; mirrors `PROJECT_MANDATE_MAX_BYTES` on the server. */
export const PROJECT_MANDATE_MAX_BYTES = 32_000;
/** Above this size the mandate dialog notes that every project session pays for it. */
export const PROJECT_MANDATE_LARGE_BYTES = 16_000;

/** Most UTF-8 bytes a description may hold; mirrors `PROJECT_DESCRIPTION_MAX_BYTES`. */
export const PROJECT_DESCRIPTION_MAX_BYTES = 8_000;

interface ProjectListResponse {
  object: "list";
  data: Project[];
}

async function readError(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { error?: { message?: string }; message?: string };
    return body.error?.message ?? body.message ?? `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

/** List the caller's projects in their preferred display order. */
export async function listProjects(): Promise<Project[]> {
  const res = await authenticatedFetch("/v1/projects");
  if (!res.ok) throw new Error(await readError(res));
  const body = (await res.json()) as ProjectListResponse;
  return body.data;
}

/** Fetch a single project (including its `config`) by id. 404s if not owned. */
export async function getProject(id: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`);
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as Project;
}

/**
 * Create a project, optionally with initial `config` defaults. Rejects with the
 * server's message on a duplicate name (409) so callers can surface it inline.
 */
export async function createProject(
  name: string,
  config?: ProjectConfig,
  idempotencyKey?: string,
  description?: string,
): Promise<Project> {
  const res = await authenticatedFetch("/v1/projects", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
    },
    body: JSON.stringify({
      name,
      ...(config ? { config } : {}),
      ...(description?.trim() ? { description } : {}),
    }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as Project;
}

/** Rename a project (O(1) — members reference the id, not the name string). */
export async function renameProject(id: string, name: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Replace a project's stored `config` defaults. Passing `{}` clears them
 * (the server treats `{}` as "clear", distinct from omitting the field, which
 * leaves config unchanged). Only `config` is sent, so the name is untouched.
 */
/** One other project whose Lead read this board in the audit window. */
export interface BoardReadAuditEntry {
  project_id: string;
  /** Null when the viewer can't see the reading project. */
  project_name: string | null;
  reads: number;
  actions: Record<string, number>;
  last_read_at: number;
}

export interface BoardReadAudit {
  since: number;
  /** False when the server records no usage events, so no reads can happen. */
  recording: boolean;
  data: BoardReadAuditEntry[];
}

/** Which other projects' Leads read this board in the last day. */
export async function getBoardReadAudit(id: string): Promise<BoardReadAudit> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}/board-reads/audit`);
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as BoardReadAudit;
}

export async function updateProjectConfig(id: string, config: ProjectConfig): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/**
 * Owner-only: choose who receives routed events and remove or restore the Lead.
 * `router_identity_id: null` is the default (the Lead, else the first persona).
 */
export async function setProjectRouting(
  id: string,
  routing: { router_identity_id: string | null; lead_removed: boolean },
): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}/routing`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(routing),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/** Replace a project's mandate; blank text clears it. Only `mandate` is sent. */
export async function updateProjectMandate(id: string, mandate: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mandate }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/** Replace a project's description (charter for people); blank clears it. Owner-only. */
export async function updateProjectDescription(id: string, description: string): Promise<Project> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

function mandateProposalPath(projectId: string, proposalId: string): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/mandate/proposals/${encodeURIComponent(proposalId)}`;
}

/** Append a pending proposal to the mandate. */
export async function acceptMandateProposal(
  projectId: string,
  proposalId: string,
): Promise<Project> {
  const res = await authenticatedFetch(`${mandateProposalPath(projectId, proposalId)}/accept`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/** Drop a pending proposal without changing the mandate. */
export async function dismissMandateProposal(
  projectId: string,
  proposalId: string,
): Promise<Project> {
  const res = await authenticatedFetch(mandateProposalPath(projectId, proposalId), {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as Project;
}

/** Lifecycle of an identity's active session; `busy` means a turn is in flight. */
export type ProjectIdentitySessionState =
  "absent" | "missing" | "archived" | "idle" | "busy" | "active" | "unavailable";

/**
 * A named persistent role in a project, such as a PM. The built-in
 * Lead is listed first with `builtin: true` (unless the owner removed it) and
 * cannot be edited here.
 */
export interface ProjectIdentity {
  id: string;
  project_id: string;
  name: string;
  builtin: boolean;
  instructions: string;
  agent_id: string | null;
  model: string | null;
  effort: string | null;
  active_session_id: string | null;
  active_session_state: ProjectIdentitySessionState;
  /** The agent session that created it; `null` or absent when the owner did. */
  created_by_session_id?: string | null;
  /** The role's own copy of its base agent, once a session has made it. */
  project_agent_id?: string | null;
  /** Whether it receives the project's routed events (one entry at most). */
  router?: boolean;
}

/** Fields an owner edits; an empty string clears `agent_id`, `model` or `effort`. */
export interface ProjectIdentityInput {
  name?: string;
  instructions?: string;
  agent_id?: string;
  model?: string;
  effort?: string;
}

/** Most UTF-8 bytes a persona may hold; mirrors the server bound. */
export const PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES = 8_000;

function identitiesPath(projectId: string, identityId?: string): string {
  const base = `/v1/projects/${encodeURIComponent(projectId)}/identities`;
  return identityId ? `${base}/${encodeURIComponent(identityId)}` : base;
}

/** List the project's identities, the built-in Lead first. */
export async function listProjectIdentities(projectId: string): Promise<ProjectIdentity[]> {
  const res = await authenticatedFetch(identitiesPath(projectId));
  if (!res.ok) throw await apiErrorFromResponse(res);
  return ((await res.json()) as { data: ProjectIdentity[] }).data;
}

export async function createProjectIdentity(
  projectId: string,
  input: ProjectIdentityInput,
): Promise<ProjectIdentity> {
  const res = await authenticatedFetch(identitiesPath(projectId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectIdentity;
}

export async function updateProjectIdentity(
  projectId: string,
  identityId: string,
  input: ProjectIdentityInput,
): Promise<ProjectIdentity> {
  const res = await authenticatedFetch(identitiesPath(projectId, identityId), {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as ProjectIdentity;
}

export async function deleteProjectIdentity(projectId: string, identityId: string): Promise<void> {
  const res = await authenticatedFetch(identitiesPath(projectId, identityId), {
    method: "DELETE",
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
}

/**
 * Open an identity's session: its live one, or a new one when it has none.
 * `expectedSessionId` is the session the caller saw, so double clicks never
 * start two sessions for one identity.
 */
export async function openProjectIdentitySession(
  projectId: string,
  identityId: string,
  expectedSessionId: string | null,
): Promise<{ session_id: string; created: boolean }> {
  const res = await authenticatedFetch(`${identitiesPath(projectId, identityId)}/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_session_id: expectedSessionId }),
  });
  if (!res.ok) throw await apiErrorFromResponse(res);
  return (await res.json()) as { session_id: string; created: boolean };
}

/**
 * Delete a project. Only the container is removed; member sessions are kept
 * (never cascade-deleted). Their `project_id` is left dangling server-side, but
 * the dual-read listing joins against the (now-absent) project, so they surface
 * as unfiled. Returns 404 if not found / not owned.
 */
export async function deleteProject(id: string): Promise<void> {
  const res = await authenticatedFetch(`/v1/projects/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await readError(res));
}

export interface ProjectOrder {
  ordered_project_ids: string[] | null;
  sort_mode: "alphabetical" | "manual";
}

export async function getProjectOrder(): Promise<ProjectOrder> {
  const res = await authenticatedFetch("/v1/projects/order");
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as ProjectOrder;
}

export async function saveProjectOrder(ids: string[] | null): Promise<ProjectOrder> {
  const res = await authenticatedFetch("/v1/projects/order", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ordered_project_ids: ids }),
  });
  if (!res.ok) throw new Error(await readError(res));
  return (await res.json()) as ProjectOrder;
}

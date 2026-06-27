/**
 * Client for the GTM control-plane HTTP API (``/v1/control-plane/*``).
 *
 * The control plane is a deploy-layer wrapper (Databricks Apps) that
 * sits in front of upstream Omnigent. It governs agent visibility,
 * delegated publishing, org-wide usage, and an audit log, keyed off the
 * caller's resolved role (admin / contributor / consumer).
 *
 * Mirrors :mod:`accountsApi` — every helper resolves with a typed
 * discriminated union (``{ ok: true, ... } | { ok: false, error }``)
 * on non-2xx instead of throwing, so the AdminPage can render specific
 * messages (403 vs 404 vs duplicate-name vs network failure) without a
 * try/catch at every call site.
 *
 * All requests go through :func:`authenticatedFetch`, which injects the
 * ``X-Forwarded-Email`` header + ``cache: 'no-store'``. The control
 * plane resolves identity from that header (the Databricks Apps proxy
 * sets it in production).
 *
 * The control plane is OPTIONAL: it only exists on the Databricks Apps
 * deploy. On every other build (OSS, header / OIDC) the routes 404. The
 * read helpers surface that as ``status: 404`` so the AdminPage can show
 * a graceful "not available in this deployment" state rather than crash.
 */

import { authenticatedFetch } from "./identity";

const BASE = "/v1/control-plane";

// ── Shared shapes ──────────────────────────────────────────────────

/** Caller's resolved role. Drives which AdminPage sections render. */
export type ControlPlaneRole = "admin" | "contributor" | "consumer";

/** Agent visibility scope. */
export type AgentVisibility = "org" | "restricted";

/** The set of users + groups an agent is restricted to (when restricted). */
export interface Audience {
  users: string[];
  groups: string[];
}

/** Per-capability flags returned by ``GET /me``. */
export interface Capabilities {
  can_publish: boolean;
  can_manage_visibility: boolean;
  can_view_usage: boolean;
  can_manage_all: boolean;
}

/** Response shape of ``GET /v1/control-plane/me``. */
export interface ControlPlaneMe {
  user_id: string;
  role: ControlPlaneRole;
  groups: string[];
  is_platform_admin: boolean;
  capabilities: Capabilities;
}

/** One row of the agent management list (``GET /agents`` / PATCH response). */
export interface ManagedAgent {
  id: string;
  name: string;
  description: string | null;
  visibility: AgentVisibility;
  audience: Audience;
  owner_id: string;
  created_at: number;
  viewer_can_manage: boolean;
}

/** One session-scoped agent eligible to publish (``GET /publishable``). */
export interface PublishableAgent {
  session_id: string;
  agent_id: string;
  name: string;
  title: string;
}

/** Response of ``POST /agents/publish``. */
export interface PublishedAgent {
  agent_id: string;
  name: string;
  owner_id: string;
  visibility: AgentVisibility;
}

/** Per-user usage breakdown nested in a usage row. */
export interface UsageByUser {
  user_id: string;
  cost_usd: number;
  total_tokens: number;
  session_count: number;
}

/** Per-agent usage row (``GET /usage``). */
export interface UsageRow {
  agent_id: string;
  agent_name: string;
  total_cost_usd: number;
  total_tokens: number;
  session_count: number;
  by_user: UsageByUser[];
}

/** Aggregate totals row returned alongside the usage rows. */
export interface UsageTotals {
  total_cost_usd: number;
  total_tokens: number;
  session_count: number;
}

/** Full ``GET /usage`` payload. */
export interface UsageReport {
  data: UsageRow[];
  totals: UsageTotals;
}

/** One audit-log entry (``GET /audit``). */
export interface AuditEntry {
  id: number;
  ts: number;
  actor: string;
  action: string;
  agent_id: string;
  detail: string;
}

/**
 * Typed failure shared by every helper. ``status`` is the HTTP status
 * (``0`` for a network failure), so the UI can branch on it — e.g. 404
 * means "control plane not present in this deploy", 409 means duplicate
 * name on publish.
 */
export interface ControlPlaneFailure {
  ok: false;
  error: string;
  status: number;
}

export type ControlPlaneResult<T> = ({ ok: true } & T) | ControlPlaneFailure;

// ── Internal helpers ───────────────────────────────────────────────

/** Map a non-2xx response to a user-facing message. */
async function _errorMessage(res: Response): Promise<string> {
  if (res.status === 401) return "You need to sign in to view this.";
  if (res.status === 403) return "You don't have permission for this.";
  if (res.status === 404) return "Not found.";
  let message = `Request failed (${res.status}).`;
  if (res.status >= 500) return "Server error. Try again in a moment.";
  try {
    const data = (await res.json()) as { error?: string; detail?: string };
    if (data.error) message = data.error;
    else if (data.detail) message = data.detail;
  } catch {
    // Body wasn't JSON; keep the generic message.
  }
  return message;
}

/**
 * Run a request and unwrap the JSON body on success, mapping non-2xx to
 * a typed :type:`ControlPlaneFailure`. ``toSuccess`` shapes the parsed
 * body into the success payload (sans the ``ok`` discriminant).
 */
async function _request<T>(
  doFetch: () => Promise<Response>,
  toSuccess: (body: unknown) => T,
): Promise<ControlPlaneResult<T>> {
  let res: Response;
  try {
    res = await doFetch();
  } catch {
    return { ok: false, error: "Could not reach the server.", status: 0 };
  }
  if (res.ok) {
    // A 204 (e.g. DELETE) has no body; don't try to parse it.
    const body = res.status === 204 ? {} : await res.json();
    return { ok: true, ...toSuccess(body) } as ControlPlaneResult<T>;
  }
  return { ok: false, error: await _errorMessage(res), status: res.status };
}

function _jsonBody(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  };
}

// ── Public API ─────────────────────────────────────────────────────

/**
 * GET /v1/control-plane/me — the caller's resolved role + capabilities.
 *
 * 404 (returned as ``status: 404``) means the control plane isn't part
 * of this deployment — the AdminPage uses that to render a graceful
 * "not available" state instead of crashing.
 */
export async function getControlPlaneMe(): Promise<ControlPlaneResult<{ me: ControlPlaneMe }>> {
  return _request(
    () => authenticatedFetch(`${BASE}/me`),
    (body) => ({ me: body as ControlPlaneMe }),
  );
}

/** GET /v1/control-plane/agents — management list (admin/contributor). */
export async function listControlPlaneAgents(): Promise<
  ControlPlaneResult<{ agents: ManagedAgent[] }>
> {
  return _request(
    () => authenticatedFetch(`${BASE}/agents`),
    (body) => ({ agents: (body as { data: ManagedAgent[] }).data }),
  );
}

/**
 * PATCH /v1/control-plane/agents/{id}/visibility — set an agent's
 * visibility + audience. Admin (any agent) or owner (own agent).
 */
export async function setAgentVisibility(
  agentId: string,
  visibility: AgentVisibility,
  audience: Audience,
): Promise<ControlPlaneResult<{ agent: ManagedAgent }>> {
  return _request(
    () =>
      authenticatedFetch(`${BASE}/agents/${encodeURIComponent(agentId)}/visibility`, {
        ..._jsonBody({ visibility, audience }),
        method: "PATCH",
      }),
    (body) => ({ agent: body as ManagedAgent }),
  );
}

/** GET /v1/control-plane/publishable — the caller's publishable agents. */
export async function listPublishable(): Promise<
  ControlPlaneResult<{ publishable: PublishableAgent[] }>
> {
  return _request(
    () => authenticatedFetch(`${BASE}/publishable`),
    (body) => ({ publishable: (body as { data: PublishableAgent[] }).data }),
  );
}

/** Body of POST /v1/control-plane/agents/publish. */
export interface PublishRequest {
  source_session_id: string;
  name: string;
  description: string;
  visibility: AgentVisibility;
  audience: Audience;
}

/**
 * POST /v1/control-plane/agents/publish — promote a session-scoped
 * agent into the shared catalog. Contributor+ only.
 *
 * A duplicate template name surfaces as ``status: 409`` so the publish
 * dialog can show a clear "name already taken" message.
 */
export async function publishAgent(
  body: PublishRequest,
): Promise<ControlPlaneResult<{ published: PublishedAgent }>> {
  return _request(
    () => authenticatedFetch(`${BASE}/agents/publish`, _jsonBody(body)),
    (resBody) => ({ published: resBody as PublishedAgent }),
  );
}

/**
 * GET /v1/control-plane/usage — per-agent usage + cost (admin/contributor).
 *
 * :param agentId: Optional single-agent drill-down (``?agent_id=``).
 */
export async function getUsage(
  agentId?: string,
): Promise<ControlPlaneResult<{ report: UsageReport }>> {
  const qs = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  return _request(
    () => authenticatedFetch(`${BASE}/usage${qs}`),
    (body) => ({ report: body as UsageReport }),
  );
}

/** GET /v1/control-plane/audit — recent governed actions (admin only). */
export async function getAudit(): Promise<ControlPlaneResult<{ entries: AuditEntry[] }>> {
  return _request(
    () => authenticatedFetch(`${BASE}/audit`),
    (body) => ({ entries: (body as { data: AuditEntry[] }).data }),
  );
}

/**
 * DELETE /v1/control-plane/agents/{id} — delete a custom (template) agent.
 * Admin (any) or owner (own). 403 for a non-owner non-admin, 404 if unknown.
 */
export async function deleteControlPlaneAgent(
  agentId: string,
): Promise<ControlPlaneResult<{ deleted: true }>> {
  return _request(
    () => authenticatedFetch(`${BASE}/agents/${encodeURIComponent(agentId)}`, { method: "DELETE" }),
    () => ({ deleted: true as const }),
  );
}

/** One check row in an agent connection-test result. */
export interface AgentTestCheck {
  name: string;
  ok: boolean;
  detail: string;
}

/** Response of ``POST /agents/{id}/test`` and ``/agents/validate-bundle``. */
export interface AgentTestResult {
  ok: boolean;
  agent_id: string | null;
  harness: string | null;
  model: string | null;
  mcp_server_count: number | null;
  checks: AgentTestCheck[];
}

/**
 * POST /v1/control-plane/agents/{id}/test — quick connectivity / launchability
 * check (record resolves, bundle present + loadable, spec valid). Authorized
 * to anyone who can view the agent.
 */
export async function testAgent(
  agentId: string,
): Promise<ControlPlaneResult<{ result: AgentTestResult }>> {
  return _request(
    () => authenticatedFetch(`${BASE}/agents/${encodeURIComponent(agentId)}/test`, { method: "POST" }),
    (body) => ({ result: body as AgentTestResult }),
  );
}

/**
 * POST /v1/control-plane/agents/validate-bundle — dry-run smoke test for a
 * not-yet-created custom agent bundle. Validates the raw bundle bytes (parse +
 * spec validation) WITHOUT persisting anything, so the composer can preflight
 * a custom agent before launching. Returns the same shape as {@link testAgent}.
 *
 * Returns ``status: 404`` when the control plane isn't part of the deployment
 * (OSS / non-Databricks-Apps), so callers can treat validation as optional.
 */
export async function validateAgentBundle(
  bundle: File,
): Promise<ControlPlaneResult<{ result: AgentTestResult }>> {
  const form = new FormData();
  form.append("bundle", bundle);
  return _request(
    () => authenticatedFetch(`${BASE}/agents/validate-bundle`, { method: "POST", body: form }),
    (body) => ({ result: body as AgentTestResult }),
  );
}

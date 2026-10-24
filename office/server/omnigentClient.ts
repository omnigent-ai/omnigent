// Server-side Omnigent API client. Credentials live only here, never in the
// browser bundle.

import type { ItemsPage } from "./activity.ts";

export interface OmnigentProject {
  id: string;
  name: string;
  created_at?: number;
  /** Session defaults shared with Omnigent's own UI (`host_id`, `workspace`). */
  config?: { icon?: string; host_id?: string; workspace?: string };
}

export interface HostDirectoryEntry {
  name: string;
  path: string;
  type: "directory" | "file" | "other";
}

export interface OmnigentSessionListItem {
  id: string;
  title: string | null;
  agent_name: string | null;
  status: string;
  project_id: string | null;
  parent_session_id: string | null;
  created_at: number;
  updated_at: number;
  host_online: boolean | null;
  runner_online: boolean | null;
  pending_elicitations_count: number;
  archived: boolean;
  labels?: Record<string, string>;
}

export class OmnigentError extends Error {
  readonly status: number;
  readonly code: string | undefined;

  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export interface OmnigentClientOptions {
  baseUrl: string;
  token?: string;
  userEmail?: string;
  fetchImpl?: typeof fetch;
}

const SESSION_PAGE_LIMIT = 1000;
const MAX_SESSION_PAGES = 20;

export class OmnigentClient {
  readonly baseUrl: string;
  private readonly headers: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(options: OmnigentClientOptions) {
    this.baseUrl = options.baseUrl.replace(/\/+$/, "");
    this.fetchImpl = options.fetchImpl ?? fetch;
    this.headers = {};
    if (options.token) this.headers.Authorization = `Bearer ${options.token}`;
    if (options.userEmail) this.headers["X-Forwarded-Email"] = options.userEmail;
  }

  /** Headers for direct upstream connections, such as the dictation socket proxy. */
  get authHeaders(): Record<string, string> {
    return { ...this.headers };
  }

  async request<T>(path: string, init: { method?: string; body?: unknown; form?: FormData } = {}): Promise<T> {
    const headers: Record<string, string> = { ...this.headers, Accept: "application/json" };
    if (init.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      method: init.method ?? "GET",
      headers,
      body: init.form ?? (init.body === undefined ? undefined : JSON.stringify(init.body)),
      signal: AbortSignal.timeout(30_000),
    });
    const text = await response.text();
    const parsed = text ? (JSON.parse(text) as unknown) : null;
    if (!response.ok) {
      const error = (parsed as { error?: { message?: string; code?: string } } | null)?.error;
      throw new OmnigentError(
        response.status,
        error?.message ?? `${response.status} ${response.statusText}`,
        error?.code,
      );
    }
    return parsed as T;
  }

  async health(): Promise<void> {
    await this.request("/health");
  }

  async listProjects(): Promise<OmnigentProject[]> {
    return (await this.request<{ data: OmnigentProject[] }>("/v1/projects")).data;
  }

  /** Every accessible top-level, non-archived session. */
  async listSessions(): Promise<OmnigentSessionListItem[]> {
    const sessions: OmnigentSessionListItem[] = [];
    let after: string | null = null;
    for (let page = 0; page < MAX_SESSION_PAGES; page++) {
      const params = new URLSearchParams({
        limit: String(SESSION_PAGE_LIMIT),
        sort_by: "updated_at",
        visibility: "all",
      });
      if (after) params.set("after", after);
      const result = await this.request<{
        data: OmnigentSessionListItem[];
        has_more: boolean;
        last_id: string | null;
      }>(`/v1/sessions?${params}`);
      // The server omits null fields; an absent project_id means the session is unfiled.
      sessions.push(
        ...result.data.map((session) => ({
          ...session,
          project_id: session.project_id ?? null,
          parent_session_id: session.parent_session_id ?? null,
          host_online: session.host_online ?? null,
          runner_online: session.runner_online ?? null,
        })),
      );
      if (!result.has_more || !result.last_id) break;
      after = result.last_id;
    }
    return sessions;
  }

  async listItems(sessionId: string, options: { after?: string | null; limit?: number } = {}): Promise<ItemsPage> {
    const params = new URLSearchParams({ limit: String(options.limit ?? 100), order: "desc" });
    if (options.after) params.set("after", options.after);
    return this.request<ItemsPage>(`/v1/sessions/${encodeURIComponent(sessionId)}/items?${params}`);
  }

  async getSession(sessionId: string): Promise<Record<string, unknown>> {
    const params = new URLSearchParams({ include_items: "false", include_usage: "false" });
    return this.request(`/v1/sessions/${encodeURIComponent(sessionId)}?${params}`);
  }

  /** A 32-hex stableId becomes the item id, which makes retries idempotent. */
  async sendMessage(
    sessionId: string,
    text: string,
    stableId?: string,
  ): Promise<{ queued: boolean; denied?: boolean; item_id?: string }> {
    const data: Record<string, unknown> = { role: "user", content: [{ type: "input_text", text }] };
    if (stableId) data.stable_id = stableId;
    return this.request(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
      method: "POST",
      body: { type: "message", data },
    });
  }

  async createProject(name: string, config: Record<string, unknown>): Promise<OmnigentProject> {
    return this.request("/v1/projects", { method: "POST", body: { name, config } });
  }

  async createBundledSession(bundle: Buffer, metadata: Record<string, unknown>): Promise<{ session_id: string }> {
    const form = new FormData();
    form.append("metadata", JSON.stringify(metadata));
    form.append("bundle", new Blob([new Uint8Array(bundle)], { type: "application/gzip" }), "agent.tar.gz");
    return this.request("/v1/sessions", { method: "POST", form });
  }

  async bindRunner(sessionId: string, runnerId: string): Promise<void> {
    await this.request(`/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "PATCH", body: { runner_id: runnerId } });
  }

  async addSessionPolicy(sessionId: string, name: string, expression: string): Promise<{ id: string }> {
    return this.request(`/v1/sessions/${encodeURIComponent(sessionId)}/policies`, {
      method: "POST",
      body: { name, type: "python", handler: "omnigent.policies.builtins.cel.cel_policy", factory_params: { expression } },
    });
  }

  async deleteSessionPolicy(sessionId: string, policyId: string): Promise<void> {
    await this.request(`/v1/sessions/${encodeURIComponent(sessionId)}/policies/${encodeURIComponent(policyId)}`, {
      method: "DELETE",
    });
  }

  async listHosts(): Promise<unknown> {
    return this.request("/v1/hosts");
  }

  /** One page of a host directory; `path` may be absolute or `~`-prefixed. */
  async listHostDirectory(hostId: string, path: string): Promise<{ data: HostDirectoryEntry[]; has_more: boolean }> {
    const trimmed = path.trim() || "~";
    const suffix = trimmed === "~" ? "" : `/${trimmed.replace(/^\/+/, "").split("/").map(encodeURIComponent).join("/")}`;
    return this.request(`/v1/hosts/${encodeURIComponent(hostId)}/filesystem${suffix}?limit=500`);
  }

  async listHarnesses(): Promise<unknown> {
    return this.request("/v1/harnesses");
  }

  async info(): Promise<Record<string, unknown>> {
    return this.request("/v1/info");
  }

  async interrupt(sessionId: string): Promise<unknown> {
    return this.request(`/v1/sessions/${encodeURIComponent(sessionId)}/events`, {
      method: "POST",
      body: { type: "interrupt", data: {} },
    });
  }

  async resolveElicitation(
    sessionId: string,
    elicitationId: string,
    action: "accept" | "decline",
    content?: Record<string, unknown>,
  ): Promise<unknown> {
    return this.request(
      `/v1/sessions/${encodeURIComponent(sessionId)}/elicitations/${encodeURIComponent(elicitationId)}/resolve`,
      { method: "POST", body: content ? { action, content } : { action } },
    );
  }
}

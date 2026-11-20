// REST client for the thingy routes (`/v1/thingies`). Requests go through `authenticatedFetch`,
// so the viewer's own authentication and host routing apply as for the rest of the app.

import { authenticatedFetch } from "@/lib/identity";
import { ApiError, apiErrorFromResponse, SIGN_IN_EXPIRED_CODE } from "@/lib/sessionsApi";

import type {
  ThingyCapability,
  ThingySessionListRequest,
  ThingyViewerSession,
} from "../sdk/thingyProtocol";

const THINGIES_PATH = "/v1/thingies";

export interface ThingyPermissions {
  read: boolean;
  write_data: boolean;
  publish: boolean;
  manage: boolean;
}

export interface Thingy {
  id: string;
  name: string;
  owner_id: string;
  /** The caller, as an email or UPN, for display. Distinct from `owner_id`. */
  viewer_id: string;
  /** The caller's numeric principal ID, as an opaque string. Older servers omit it. */
  viewer_principal_id?: string;
  published_revision: string | null;
  version: string | null;
  permissions: ThingyPermissions;
}

export interface ThingyContent {
  revision_id: string;
  html: string;
  sdk_version: number;
  capabilities: string[];
}

/** A rule across records that the server checks on every write (see `thingies_constraints.py`). */
export type ThingyConstraint = {
  name: string;
  collection: string;
  message?: string;
} & (
  | {
      kind: "max_records";
      max: number;
      where?: Record<string, string | number | boolean>;
    }
  | { kind: "unique"; fields: string[] }
  | {
      kind: "reference";
      field: string;
      target: string;
      on_delete?: "restrict" | "cascade" | "unset";
    }
);

export interface CreateThingyRevisionRequest {
  html: string;
  sdk_version: 1;
  capabilities: ThingyCapability[];
  collections: Record<string, Record<string, unknown>>;
  /** Markdown for agents: what the collections mean, conventions and common tasks. */
  guide?: string;
  constraints?: ThingyConstraint[];
  /** Requests a viewer can make from the thingy, carried out by their thingy session. */
  actions?: {
    name: string;
    description: string;
    params?: Record<string, unknown>;
  }[];
}

export interface ThingyRpcRequest {
  protocol: 1;
  request_id: string;
  revision_id: string;
  method: string;
  params: Record<string, unknown>;
}

/**
 * A failed thingy request. Extends the app's `ApiError` with the server's request ID and error
 * details, which the frame bridge reads (for example a `REVISION_CHANGED` reason).
 */
export class ThingyApiError extends ApiError {
  readonly requestId?: string;
  readonly details?: unknown;

  constructor({
    status,
    code,
    message,
    requestId,
    details,
    retryAfterMs = null,
  }: {
    status: number;
    code: string | null;
    message: string;
    requestId?: string;
    details?: unknown;
    retryAfterMs?: number | null;
  }) {
    super(message, status, code, retryAfterMs);
    this.name = "ThingyApiError";
    this.requestId = requestId;
    this.details = details;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (text === "") {
    return undefined;
  }
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** The thingy error a failed response carries: `{error: {code, message, request_id, details}}`. */
async function thingyApiErrorFromResponse(response: Response): Promise<ThingyApiError> {
  const base = await apiErrorFromResponse(response.clone());
  const body = await readJson(response);
  const error = isRecord(body) && isRecord(body.error) ? body.error : isRecord(body) ? body : {};
  const flatCode = typeof error.code === "string" && error.code !== "" ? error.code : null;
  return new ThingyApiError({
    status: base.status,
    code: base.code ?? flatCode,
    message: base.message,
    requestId: typeof error.request_id === "string" ? error.request_id : undefined,
    details: error.details,
    retryAfterMs: base.retryAfterMs,
  });
}

async function thingyRequest(
  path: string,
  init: RequestInit & { signal?: AbortSignal } = {},
): Promise<unknown> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const response = await authenticatedFetch(path, { ...init, headers });
  if (!response.ok) {
    throw await thingyApiErrorFromResponse(response);
  }
  if (response.status === 204) {
    return undefined;
  }
  // An auth layer's login page answers with a 200 HTML page: an expired sign-in.
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.toLowerCase().includes("text/html")) {
    throw new ThingyApiError({
      status: 401,
      code: SIGN_IN_EXPIRED_CODE,
      message: "Your sign-in expired. Reload the page to sign in again.",
    });
  }
  return readJson(response);
}

function malformed(what: string): ThingyApiError {
  return new ThingyApiError({
    status: 502,
    code: "INTERNAL",
    message: `The server returned a malformed ${what}.`,
  });
}

function isStringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function parseThingy(value: unknown): Thingy {
  if (!isRecord(value) || !isRecord(value.permissions)) {
    throw malformed("app");
  }
  const { id, name, owner_id, viewer_id, viewer_principal_id, published_revision, version } = value;
  const permissions = value.permissions;
  if (
    typeof id !== "string" ||
    typeof name !== "string" ||
    typeof owner_id !== "string" ||
    typeof viewer_id !== "string" ||
    !isStringOrNull(published_revision) ||
    !isStringOrNull(version)
  ) {
    throw malformed("app");
  }
  return {
    id,
    name,
    owner_id,
    viewer_id,
    viewer_principal_id: typeof viewer_principal_id === "string" ? viewer_principal_id : undefined,
    published_revision,
    version,
    permissions: {
      read: permissions.read === true,
      write_data: permissions.write_data === true,
      publish: permissions.publish === true,
      manage: permissions.manage === true,
    },
  };
}

function parseContent(value: unknown): ThingyContent {
  if (
    !isRecord(value) ||
    typeof value.revision_id !== "string" ||
    typeof value.html !== "string" ||
    typeof value.sdk_version !== "number" ||
    !Array.isArray(value.capabilities)
  ) {
    throw malformed("app content");
  }
  return {
    revision_id: value.revision_id,
    html: value.html,
    sdk_version: value.sdk_version,
    capabilities: value.capabilities.filter(
      (capability): capability is string => typeof capability === "string",
    ),
  };
}

function appPath(appId: string, suffix = ""): string {
  return `${THINGIES_PATH}/${encodeURIComponent(appId)}${suffix}`;
}

export async function listThingies(signal?: AbortSignal): Promise<Thingy[]> {
  const body = await thingyRequest(THINGIES_PATH, { signal });
  return isRecord(body) && Array.isArray(body.items) ? body.items.map(parseThingy) : [];
}

/** Someone else's thingy that reaches the caller through their own grant or a group's. */
export interface SharedThingy extends Thingy {
  shared_via: "user" | "group";
  owner_email: string | null;
}

function parseSharedThingy(value: unknown): SharedThingy {
  const app = parseThingy(value);
  const via = isRecord(value) ? value.shared_via : undefined;
  if (via !== "user" && via !== "group") {
    throw malformed("app");
  }
  const ownerEmail =
    isRecord(value) && typeof value.owner_email === "string" ? value.owner_email : null;
  return { ...app, shared_via: via, owner_email: ownerEmail };
}

/**
 * Thingies other people own that reach the viewer through their own grant or a group's. The server
 * leaves out thingies the viewer reads only as an administrator.
 */
export async function listSharedThingies(signal?: AbortSignal): Promise<SharedThingy[]> {
  const body = await thingyRequest(`${THINGIES_PATH}/shared`, { signal });
  return isRecord(body) && Array.isArray(body.items) ? body.items.map(parseSharedThingy) : [];
}

export async function getThingy(appId: string, signal?: AbortSignal): Promise<Thingy> {
  return parseThingy(await thingyRequest(appPath(appId), { signal }));
}

export async function createThingy(name: string): Promise<Thingy> {
  return parseThingy(
    await thingyRequest(THINGIES_PATH, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  );
}

/** Deletes the thingy, with its data and versions, for everyone. Needs manage access. */
export async function deleteThingy(appId: string): Promise<void> {
  await thingyRequest(appPath(appId), { method: "DELETE" });
}

export async function createThingyRevision(
  appId: string,
  revision: CreateThingyRevisionRequest,
): Promise<{ id: string }> {
  const body = await thingyRequest(appPath(appId, "/revisions"), {
    method: "POST",
    body: JSON.stringify(revision),
  });
  if (!isRecord(body) || typeof body.id !== "string") {
    throw malformed("revision");
  }
  return { id: body.id };
}

export async function publishThingyRevision(
  appId: string,
  revisionId: string,
  ifVersion: string | null,
): Promise<Thingy> {
  const body = await thingyRequest(appPath(appId, "/publish"), {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, if_version: ifVersion }),
  });
  return parseThingy(body);
}

/** One entry of a thingy's action ledger. */
export interface ThingyActionRecord {
  id: string;
  version: string;
  action: string;
  params: Record<string, unknown>;
  state: "queued" | "running" | "done" | "failed" | "cancelled";
  /** The request joined an earlier, still open request for the same action and params. */
  merged: boolean;
  /**
   * Whether the entry has been handed to a session. A merged request that was not (its New
   * session was never started) needs its message sent again. Older servers omit it (false).
   */
  delivered: boolean;
}

/**
 * How far an action has got, derived on the server from the sessions doing the work:
 * `pending` (not delivered yet), `queued`, `running`, `needs_input`, `done`, `failed`,
 * `cancelled`, `finished_unreported` (its sessions stopped without the agent reporting), or
 * `interrupted` (a session failed, went offline, or never started it). `not_found` for an
 * unknown ID; newer servers may add statuses.
 */
export interface ThingyActionProgress {
  id: string;
  status: string;
  reason: string | null;
  action: string | null;
  params: Record<string, unknown> | null;
  summary: string | null;
  sessionId: string | null;
  sessionIds: string[];
  /** When it was requested (ISO 8601), or null from a server that does not say. */
  requestedAt: string | null;
}

/** Queues an action in the thingy's ledger; `operationId` makes a retry return the same entry. */
export async function requestThingyAction(
  appId: string,
  request: {
    action: string;
    params: Record<string, unknown>;
    operationId: string;
  },
): Promise<ThingyActionRecord> {
  const body = await thingyRequest(appPath(appId, "/actions"), {
    method: "POST",
    body: JSON.stringify({
      action: request.action,
      params: request.params,
      operation_id: request.operationId,
    }),
  });
  if (
    !isRecord(body) ||
    typeof body.id !== "string" ||
    typeof body.version !== "string" ||
    !isRecord(body.value)
  ) {
    throw malformed("action");
  }
  const value = body.value;
  return {
    id: body.id,
    version: body.version,
    action: String(value.action),
    params: isRecord(value.params) ? value.params : {},
    state: (value.state as ThingyActionRecord["state"]) ?? "queued",
    merged: body.merged === true,
    delivered: body.delivered === true,
  };
}

function parseActionProgress(value: unknown): ThingyActionProgress {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.status !== "string") {
    throw malformed("action progress");
  }
  return {
    id: value.id,
    status: value.status,
    reason: typeof value.reason === "string" ? value.reason : null,
    action: typeof value.action === "string" ? value.action : null,
    params: isRecord(value.params) ? value.params : null,
    summary: typeof value.summary === "string" ? value.summary : null,
    sessionId: typeof value.session_id === "string" ? value.session_id : null,
    sessionIds: Array.isArray(value.session_ids)
      ? value.session_ids.filter((item): item is string => typeof item === "string")
      : [],
    requestedAt: typeof value.requested_at === "string" ? value.requested_at : null,
  };
}

/** Progress of the given actions, or of the viewer's tracked actions when `ids` is empty. */
export async function getThingyActionProgress(
  appId: string,
  ids: string[],
  signal?: AbortSignal,
): Promise<ThingyActionProgress[]> {
  const query = ids.length > 0 ? `?ids=${ids.map(encodeURIComponent).join(",")}` : "";
  const body = await thingyRequest(appPath(appId, `/actions/progress${query}`), { signal });
  if (!isRecord(body) || !Array.isArray(body.items)) {
    throw malformed("action progress");
  }
  return body.items.map(parseActionProgress);
}

/** Records that the viewer's action was posted into their thingy session, so its progress can be followed. */
export async function recordThingyActionDelivery(
  appId: string,
  actionId: string,
  sessionId: string,
): Promise<void> {
  await thingyRequest(appPath(appId, `/actions/${encodeURIComponent(actionId)}/delivery`), {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

/** Renames the thingy for everyone; needs edit access to the thingy. */
export async function renameThingy(appId: string, name: string): Promise<Thingy> {
  return parseThingy(
    await thingyRequest(appPath(appId, ""), {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  );
}

/**
 * Whether the viewer's thingy session (with the sessions it started) is working, as the server
 * derives it: `none`, `running`, `needs_input`, `idle`, `interrupted` or `unavailable`.
 */
export async function getThingyThingySessionStatus(
  appId: string,
  signal?: AbortSignal,
): Promise<{ sessionId: string | null; status: string }> {
  const body = await thingyRequest(appPath(appId, "/session/status"), {
    signal,
  });
  if (!isRecord(body) || typeof body.status !== "string") {
    throw malformed("thingy session status");
  }
  return {
    sessionId: typeof body.session_id === "string" ? body.session_id : null,
    status: body.status,
  };
}

/** The viewer's own thingy session for this thingy, or null before they have one. */
export async function getThingyThingySession(
  appId: string,
  signal?: AbortSignal,
): Promise<string | null> {
  const body = await thingyRequest(appPath(appId, "/session"), { signal });
  if (!isRecord(body)) {
    throw malformed("thingy session");
  }
  return typeof body.session_id === "string" ? body.session_id : null;
}

export async function setThingyThingySession(appId: string, sessionId: string): Promise<void> {
  await thingyRequest(appPath(appId, "/session"), {
    method: "PUT",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export async function getThingyContent(
  appId: string,
  signal?: AbortSignal,
): Promise<ThingyContent> {
  return parseContent(await thingyRequest(appPath(appId, "/content"), { signal }));
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** The caller's own sessions, newest activity first, in the shape a thingy sees. */
export async function listViewerSessions(
  { limit, after }: ThingySessionListRequest,
  signal?: AbortSignal,
): Promise<{ items: ThingyViewerSession[]; nextCursor: string | null }> {
  const query = new URLSearchParams({
    limit: String(limit),
    sort_by: "updated_at",
    visibility: "mine",
  });
  if (after) {
    query.set("after", after);
  }
  const body = await thingyRequest(`/v1/sessions?${query}`, { signal });
  if (!isRecord(body) || !Array.isArray(body.data)) {
    throw malformed("session list");
  }
  const items = body.data.filter(isRecord).flatMap((session) =>
    typeof session.id === "string"
      ? [
          {
            id: session.id,
            title: typeof session.title === "string" ? session.title : "",
            status: typeof session.status === "string" ? session.status : "unknown",
            harness: optionalString(session.harness),
            agentName: optionalString(session.agent_name),
            createdAt: typeof session.created_at === "number" ? session.created_at : null,
            updatedAt: typeof session.updated_at === "number" ? session.updated_at : null,
            unread: session.viewer_unread === true,
            needsInput:
              typeof session.pending_elicitations_count === "number" &&
              session.pending_elicitations_count > 0,
            backgroundWork: session.background_work_running === true,
          },
        ]
      : [],
  );
  const lastId = optionalString(body.last_id);
  return { items, nextCursor: body.has_more === true ? lastId : null };
}

export async function callThingyRpc(
  appId: string,
  rpc: ThingyRpcRequest,
  signal?: AbortSignal,
): Promise<unknown> {
  const body = await thingyRequest(appPath(appId, "/rpc"), {
    method: "POST",
    body: JSON.stringify(rpc),
    signal,
  });
  return isRecord(body) ? body.result : undefined;
}

/** A thingy the viewer pinned, named as it was when they pinned it. */
export interface PinnedThingy {
  id: string;
  name: string;
}

export interface ThingyShelf {
  pinned: PinnedThingy[];
  /** Whether the viewer owns any thingy; `listSharedThingies` lists the shared ones. */
  hasOwned: boolean;
}

function parseShelf(value: unknown): ThingyShelf {
  if (!isRecord(value) || !Array.isArray(value.pinned)) {
    throw malformed("thingy shelf");
  }
  return {
    pinned: value.pinned.flatMap((pin) =>
      isRecord(pin) && typeof pin.id === "string" && typeof pin.name === "string"
        ? [{ id: pin.id, name: pin.name }]
        : [],
    ),
    hasOwned: value.has_owned === true,
  };
}

/** The viewer's pinned thingies, for the sidebar. */
export async function getThingyShelf(signal?: AbortSignal): Promise<ThingyShelf> {
  return parseShelf(await thingyRequest(`${THINGIES_PATH}/shelf`, { signal }));
}

/** Pins or unpins a thingy for the viewer only. */
export async function pinThingy(appId: string, pinned: boolean): Promise<ThingyShelf> {
  return parseShelf(
    await thingyRequest(appPath(appId, "/pin"), {
      method: "PUT",
      body: JSON.stringify({ pinned }),
    }),
  );
}

function parseSessionThingies(value: unknown): Thingy[] {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw malformed("session thingies");
  }
  return value.items.map(parseThingy);
}

/** The thingies the viewer shows beside a session, in the order they added them. */
export async function getSessionThingies(
  sessionId: string,
  signal?: AbortSignal,
): Promise<Thingy[]> {
  return parseSessionThingies(
    await thingyRequest(`${THINGIES_PATH}/sessions/${encodeURIComponent(sessionId)}`, { signal }),
  );
}

/**
 * Shows a thingy beside a session or stops showing it there. Showing it also sends the thingy's
 * actions from its own page to that session when the viewer can edit the thingy.
 */
export async function linkThingyToSession(
  appId: string,
  sessionId: string,
  linked: boolean,
): Promise<Thingy[]> {
  return parseSessionThingies(
    await thingyRequest(appPath(appId, `/sessions/${encodeURIComponent(sessionId)}`), {
      method: "PUT",
      body: JSON.stringify({ linked }),
    }),
  );
}

/** Where a comment points in the thingy's page: the selected text and, if set, its record. */
export interface ThingyCommentAnchor {
  text: string;
  label?: string | null;
  ref?: string | null;
  occurrence?: number;
}

export interface ThingyComment {
  id: string;
  body: string;
  status: "draft" | "addressed";
  anchor: ThingyCommentAnchor;
  createdBy: string;
  /** Unix seconds. */
  createdAt: number;
  updatedAt: number;
}

function parseComment(value: unknown): ThingyComment {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.body !== "string" ||
    (value.status !== "draft" && value.status !== "addressed") ||
    !isRecord(value.anchor) ||
    typeof value.anchor.text !== "string"
  ) {
    throw malformed("comment");
  }
  const anchor = value.anchor;
  return {
    id: value.id,
    body: value.body,
    status: value.status,
    anchor: {
      text: String(anchor.text),
      label: typeof anchor.label === "string" ? anchor.label : null,
      ref: typeof anchor.ref === "string" ? anchor.ref : null,
      occurrence: typeof anchor.occurrence === "number" ? anchor.occurrence : 1,
    },
    createdBy: typeof value.created_by === "string" ? value.created_by : "",
    createdAt: typeof value.created_at === "number" ? value.created_at : 0,
    updatedAt: typeof value.updated_at === "number" ? value.updated_at : 0,
  };
}

export async function listThingyComments(
  appId: string,
  signal?: AbortSignal,
): Promise<{ items: ThingyComment[]; canComment: boolean }> {
  const body = await thingyRequest(appPath(appId, "/comments"), { signal });
  if (!isRecord(body) || !Array.isArray(body.items)) {
    throw malformed("comment list");
  }
  return { items: body.items.map(parseComment), canComment: body.can_comment === true };
}

export async function createThingyComment(
  appId: string,
  { body, anchor, operationId }: { body: string; anchor: ThingyCommentAnchor; operationId: string },
): Promise<ThingyComment> {
  return parseComment(
    await thingyRequest(appPath(appId, "/comments"), {
      method: "POST",
      body: JSON.stringify({ body, anchor, operation_id: operationId }),
    }),
  );
}

export async function updateThingyComment(
  appId: string,
  commentId: string,
  change: { body?: string; status?: "draft" | "addressed" },
): Promise<ThingyComment> {
  return parseComment(
    await thingyRequest(appPath(appId, `/comments/${encodeURIComponent(commentId)}`), {
      method: "PATCH",
      body: JSON.stringify(change),
    }),
  );
}

export async function deleteThingyComment(appId: string, commentId: string): Promise<void> {
  await thingyRequest(appPath(appId, `/comments/${encodeURIComponent(commentId)}`), {
    method: "DELETE",
  });
}

/**
 * One grant on a thingy, shaped like a workflow's. `level` is 1 (Can view), 2 (Can edit) or
 * 3 (Can manage), the levels in `@/lib/permissionsApi`.
 */
export interface ThingyPermission {
  user_id: string;
  thingy_id: string;
  level: number;
  /** The grantee's name for display; the ID when the server has no other name. */
  display_name: string;
}

function permissionsPath(appId: string, suffix = ""): string {
  return appPath(appId, `/permissions${suffix}`);
}

function parsePermission(value: unknown): ThingyPermission {
  if (
    !isRecord(value) ||
    typeof value.user_id !== "string" ||
    typeof value.level !== "number" ||
    !Number.isInteger(value.level)
  ) {
    throw malformed("permission");
  }
  return {
    user_id: value.user_id,
    thingy_id: typeof value.thingy_id === "string" ? value.thingy_id : "",
    level: value.level,
    display_name: typeof value.display_name === "string" ? value.display_name : value.user_id,
  };
}

/** Everyone the thingy is shared with, following every page. Needs manage access. */
export async function listThingyPermissions(
  appId: string,
  signal?: AbortSignal,
): Promise<ThingyPermission[]> {
  const all: ThingyPermission[] = [];
  let after: string | null = null;
  // Each page provides the cursor for the next request.
  /* oxlint-disable no-await-in-loop */
  do {
    const query: string = after !== null ? `?after=${encodeURIComponent(after)}` : "";
    const body = await thingyRequest(permissionsPath(appId, query), { signal });
    if (!isRecord(body) || !Array.isArray(body.permissions)) {
      throw malformed("permission list");
    }
    all.push(...body.permissions.map(parsePermission));
    after =
      typeof body.next_cursor === "string" && body.next_cursor !== "" ? body.next_cursor : null;
  } while (after !== null);
  /* oxlint-enable no-await-in-loop */
  return all;
}

/**
 * Grants a user or group access to the thingy, replacing an existing grant. With
 * `ensureMinimum`, an existing higher grant is kept. Needs manage access.
 */
export async function grantThingyPermission(
  appId: string,
  userId: string,
  level: number,
  ensureMinimum = false,
): Promise<ThingyPermission> {
  return parsePermission(
    await thingyRequest(permissionsPath(appId), {
      method: "PUT",
      body: JSON.stringify({
        user_id: userId,
        level,
        ...(ensureMinimum ? { ensure_minimum: true } : {}),
      }),
    }),
  );
}

/** Removes a grant on the thingy; a missing one is harmless. Needs manage access. */
export async function revokeThingyPermission(appId: string, userId: string): Promise<void> {
  await thingyRequest(permissionsPath(appId, `/${encodeURIComponent(userId)}`), {
    method: "DELETE",
  });
}

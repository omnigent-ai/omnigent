// REST client for the gadget routes (`/v1/gadgets`). Requests go through `authenticatedFetch`,
// so the viewer's own authentication and host routing apply as for the rest of the app.

import { authenticatedFetch } from "@/lib/identity";
import { ApiError, apiErrorFromResponse, SIGN_IN_EXPIRED_CODE } from "@/lib/sessionsApi";

import type {
  GadgetCapability,
  GadgetSessionListRequest,
  GadgetViewerSession,
} from "../sdk/gadgetProtocol";

const GADGETS_PATH = "/v1/gadgets";

export interface GadgetPermissions {
  read: boolean;
  write_data: boolean;
  publish: boolean;
  manage: boolean;
}

export interface Gadget {
  id: string;
  name: string;
  owner_id: string;
  /** The caller, as an email or UPN, for display. Distinct from `owner_id`. */
  viewer_id: string;
  /** The caller's numeric principal ID, as an opaque string. Older servers omit it. */
  viewer_principal_id?: string;
  published_revision: string | null;
  version: string | null;
  permissions: GadgetPermissions;
  /** Epoch seconds; older servers omit it. */
  created_at?: number;
}

export interface GadgetContent {
  revision_id: string;
  html: string;
  sdk_version: number;
  capabilities: string[];
  /**
   * Who gadget code sees as the viewer: an ID that is stable within this gadget and the same
   * one its records name as their authors, and a name only when the server has a real one
   * (never a login or email). Older servers omit it.
   */
  viewer?: { id: string; display_name: string | null };
}

/** A rule across records that the server checks on every write (see `gadgets_constraints.py`). */
export type GadgetConstraint = {
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

export interface CreateGadgetRevisionRequest {
  html: string;
  sdk_version: 1;
  capabilities: GadgetCapability[];
  collections: Record<string, Record<string, unknown>>;
  /** Markdown for agents: what the collections mean, conventions and common tasks. */
  guide?: string;
  constraints?: GadgetConstraint[];
  /** Requests a viewer can make from the gadget, carried out by their gadget session. */
  actions?: {
    name: string;
    description: string;
    params?: Record<string, unknown>;
  }[];
}

export interface GadgetRpcRequest {
  protocol: 1;
  request_id: string;
  revision_id: string;
  method: string;
  params: Record<string, unknown>;
}

/**
 * A failed gadget request. Extends the app's `ApiError` with the server's request ID and error
 * details, which the frame bridge reads (for example a `REVISION_CHANGED` reason).
 */
export class GadgetApiError extends ApiError {
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
    this.name = "GadgetApiError";
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

/** The gadget error a failed response carries: `{error: {code, message, request_id, details}}`. */
async function gadgetApiErrorFromResponse(response: Response): Promise<GadgetApiError> {
  const base = await apiErrorFromResponse(response.clone());
  const body = await readJson(response);
  const error = isRecord(body) && isRecord(body.error) ? body.error : isRecord(body) ? body : {};
  const flatCode = typeof error.code === "string" && error.code !== "" ? error.code : null;
  return new GadgetApiError({
    status: base.status,
    code: base.code ?? flatCode,
    message: base.message,
    requestId: typeof error.request_id === "string" ? error.request_id : undefined,
    details: error.details,
    retryAfterMs: base.retryAfterMs,
  });
}

async function gadgetRequest(
  path: string,
  init: RequestInit & { signal?: AbortSignal } = {},
): Promise<unknown> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const response = await authenticatedFetch(path, { ...init, headers });
  if (!response.ok) {
    throw await gadgetApiErrorFromResponse(response);
  }
  if (response.status === 204) {
    return undefined;
  }
  // An auth layer's login page answers with a 200 HTML page: an expired sign-in.
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.toLowerCase().includes("text/html")) {
    throw new GadgetApiError({
      status: 401,
      code: SIGN_IN_EXPIRED_CODE,
      message: "Your sign-in expired. Reload the page to sign in again.",
    });
  }
  return readJson(response);
}

function malformed(what: string): GadgetApiError {
  return new GadgetApiError({
    status: 502,
    code: "INTERNAL",
    message: `The server returned a malformed ${what}.`,
  });
}

function isStringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function parseGadget(value: unknown): Gadget {
  if (!isRecord(value) || !isRecord(value.permissions)) {
    throw malformed("app");
  }
  const { id, name, owner_id, viewer_id, viewer_principal_id, published_revision, version } = value;
  const createdAt = typeof value.created_at === "number" ? { created_at: value.created_at } : {};
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
    ...createdAt,
  };
}

function parseContent(value: unknown): GadgetContent {
  if (
    !isRecord(value) ||
    typeof value.revision_id !== "string" ||
    typeof value.html !== "string" ||
    typeof value.sdk_version !== "number" ||
    !Array.isArray(value.capabilities)
  ) {
    throw malformed("app content");
  }
  const viewer = isRecord(value.viewer) ? value.viewer : null;
  return {
    revision_id: value.revision_id,
    html: value.html,
    sdk_version: value.sdk_version,
    capabilities: value.capabilities.filter(
      (capability): capability is string => typeof capability === "string",
    ),
    ...(viewer && typeof viewer.id === "string" && viewer.id !== ""
      ? {
          viewer: {
            id: viewer.id,
            display_name: typeof viewer.display_name === "string" ? viewer.display_name : null,
          },
        }
      : {}),
  };
}

function appPath(appId: string, suffix = ""): string {
  return `${GADGETS_PATH}/${encodeURIComponent(appId)}${suffix}`;
}

// Lists page with a cursor and a page can be short or empty, so a long list of large records
// takes many pages: they are followed until the last, and only a cursor seen before (a loop)
// is malformed. This bound only stops a server that never ends a list.
const MAX_LIST_PAGES = 100_000;
const LIST_PAGE_SIZE = 100;

/** Every page of a cursor-paged list, with each page's raw body for its other fields. */
async function everyPage(
  path: string,
  query: Record<string, string>,
  signal: AbortSignal | undefined,
  what: string,
  { key = "items", limit = LIST_PAGE_SIZE }: { key?: string; limit?: number } = {},
): Promise<{ items: unknown[]; first: Record<string, unknown> | null }> {
  const items: unknown[] = [];
  let first: Record<string, unknown> | null = null;
  let cursor: string | null = null;
  const seen = new Set<string>();
  for (let page = 0; page < MAX_LIST_PAGES; page++) {
    const params = new URLSearchParams({ ...query, limit: String(limit) });
    if (cursor !== null) params.set("cursor", cursor);
    // Each page needs the previous page's cursor.
    // oxlint-disable-next-line no-await-in-loop
    const body = await gadgetRequest(`${path}?${params}`, { signal });
    const pageItems = isRecord(body) ? body[key] : undefined;
    if (!isRecord(body) || !Array.isArray(pageItems)) {
      throw malformed(what);
    }
    first ??= body;
    items.push(...pageItems);
    cursor = typeof body.next_cursor === "string" ? body.next_cursor : null;
    if (cursor === null) return { items, first };
    if (seen.has(cursor)) throw malformed(what);
    seen.add(cursor);
  }
  throw malformed(what);
}

/** The viewer's own gadgets, oldest first. */
export async function listGadgets(signal?: AbortSignal): Promise<Gadget[]> {
  const { items } = await everyPage(GADGETS_PATH, {}, signal, "app list");
  return items
    .map(parseGadget)
    .sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0) || a.id.localeCompare(b.id));
}

/** Someone else's gadget that reaches the caller through their own grant or a group's. */
export interface SharedGadget extends Gadget {
  shared_via: "user" | "group";
  owner_email: string | null;
}

function parseSharedGadget(value: unknown): SharedGadget {
  const app = parseGadget(value);
  const via = isRecord(value) ? value.shared_via : undefined;
  if (via !== "user" && via !== "group") {
    throw malformed("app");
  }
  const ownerEmail =
    isRecord(value) && typeof value.owner_email === "string" ? value.owner_email : null;
  return { ...app, shared_via: via, owner_email: ownerEmail };
}

/**
 * Gadgets other people own that reach the viewer through their own grant or a group's. The server
 * leaves out gadgets the viewer reads only as an administrator.
 */
export async function listSharedGadgets(signal?: AbortSignal): Promise<SharedGadget[]> {
  const { items } = await everyPage(`${GADGETS_PATH}/shared`, {}, signal, "shared app list");
  return items.map(parseSharedGadget);
}

export async function getGadget(appId: string, signal?: AbortSignal): Promise<Gadget> {
  return parseGadget(await gadgetRequest(appPath(appId), { signal }));
}

export async function createGadget(name: string): Promise<Gadget> {
  return parseGadget(
    await gadgetRequest(GADGETS_PATH, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  );
}

/** Deletes the gadget, with its data and versions, for everyone. Needs manage access. */
export async function deleteGadget(appId: string): Promise<void> {
  await gadgetRequest(appPath(appId), { method: "DELETE" });
}

/**
 * Deletes a gadget only while nothing was built from it (never published, not taken by the
 * viewer's gadget session), checked on the server together with the delete. Resolves whether
 * it was deleted.
 */
export async function discardUnbuiltGadget(appId: string): Promise<boolean> {
  const body = await gadgetRequest(`${appPath(appId)}?if_unbuilt=true`, { method: "DELETE" });
  return isRecord(body) && body.deleted === true;
}

export async function createGadgetRevision(
  appId: string,
  revision: CreateGadgetRevisionRequest,
): Promise<{ id: string }> {
  const body = await gadgetRequest(appPath(appId, "/revisions"), {
    method: "POST",
    body: JSON.stringify(revision),
  });
  if (!isRecord(body) || typeof body.id !== "string") {
    throw malformed("revision");
  }
  return { id: body.id };
}

export async function publishGadgetRevision(
  appId: string,
  revisionId: string,
  ifVersion: string | null,
): Promise<Gadget> {
  const body = await gadgetRequest(appPath(appId, "/publish"), {
    method: "POST",
    body: JSON.stringify({ revision_id: revisionId, if_version: ifVersion }),
  });
  return parseGadget(body);
}

/** One entry of a gadget's action ledger. */
export interface GadgetActionRecord {
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
export interface GadgetActionProgress {
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

/** Queues an action in the gadget's ledger; `operationId` makes a retry return the same entry. */
export async function requestGadgetAction(
  appId: string,
  request: {
    action: string;
    params: Record<string, unknown>;
    operationId: string;
  },
): Promise<GadgetActionRecord> {
  const body = await gadgetRequest(appPath(appId, "/actions"), {
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
    state: (value.state as GadgetActionRecord["state"]) ?? "queued",
    merged: body.merged === true,
    delivered: body.delivered === true,
  };
}

function parseActionProgress(value: unknown): GadgetActionProgress {
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
export async function getGadgetActionProgress(
  appId: string,
  ids: string[],
  signal?: AbortSignal,
): Promise<GadgetActionProgress[]> {
  const query = ids.length > 0 ? `?ids=${ids.map(encodeURIComponent).join(",")}` : "";
  const body = await gadgetRequest(appPath(appId, `/actions/progress${query}`), { signal });
  if (!isRecord(body) || !Array.isArray(body.items)) {
    throw malformed("action progress");
  }
  return body.items.map(parseActionProgress);
}

/** Records that the viewer's action was posted into their gadget session, so its progress can be followed. */
export async function recordGadgetActionDelivery(
  appId: string,
  actionId: string,
  sessionId: string,
): Promise<void> {
  await gadgetRequest(appPath(appId, `/actions/${encodeURIComponent(actionId)}/delivery`), {
    method: "POST",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

/** Renames the gadget for everyone; needs edit access to the gadget. */
export async function renameGadget(appId: string, name: string): Promise<Gadget> {
  return parseGadget(
    await gadgetRequest(appPath(appId, ""), {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  );
}

/**
 * Whether the viewer's gadget session (with the sessions it started) is working, as the server
 * derives it: `none`, `running`, `needs_input`, `idle`, `interrupted`, `unavailable`, or
 * `unknown` when not every session in its tree could be read.
 */
export async function getGadgetGadgetSessionStatus(
  appId: string,
  signal?: AbortSignal,
): Promise<{ sessionId: string | null; status: string }> {
  const body = await gadgetRequest(appPath(appId, "/session/status"), {
    signal,
  });
  if (!isRecord(body) || typeof body.status !== "string") {
    throw malformed("gadget session status");
  }
  return {
    sessionId: typeof body.session_id === "string" ? body.session_id : null,
    status: body.status,
  };
}

/** The viewer's own gadget session for this gadget, or null before they have one. */
export async function getGadgetGadgetSession(
  appId: string,
  signal?: AbortSignal,
): Promise<string | null> {
  const body = await gadgetRequest(appPath(appId, "/session"), { signal });
  if (!isRecord(body)) {
    throw malformed("gadget session");
  }
  return typeof body.session_id === "string" ? body.session_id : null;
}

export async function setGadgetGadgetSession(appId: string, sessionId: string): Promise<void> {
  await gadgetRequest(appPath(appId, "/session"), {
    method: "PUT",
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export async function getGadgetContent(
  appId: string,
  signal?: AbortSignal,
): Promise<GadgetContent> {
  return parseContent(await gadgetRequest(appPath(appId, "/content"), { signal }));
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** The caller's own sessions, newest activity first, in the shape a gadget sees. */
export async function listViewerSessions(
  { limit, after }: GadgetSessionListRequest,
  signal?: AbortSignal,
): Promise<{ items: GadgetViewerSession[]; nextCursor: string | null }> {
  const query = new URLSearchParams({
    limit: String(limit),
    sort_by: "updated_at",
    visibility: "mine",
  });
  if (after) {
    query.set("after", after);
  }
  const body = await gadgetRequest(`/v1/sessions?${query}`, { signal });
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

export async function callGadgetRpc(
  appId: string,
  rpc: GadgetRpcRequest,
  signal?: AbortSignal,
): Promise<unknown> {
  const body = await gadgetRequest(appPath(appId, "/rpc"), {
    method: "POST",
    body: JSON.stringify(rpc),
    signal,
  });
  return isRecord(body) ? body.result : undefined;
}

/** A gadget the viewer pinned, named as it was when they pinned it. */
export interface PinnedGadget {
  id: string;
  name: string;
}

export interface GadgetShelf {
  pinned: PinnedGadget[];
  /** Whether the viewer owns any gadget; `listSharedGadgets` lists the shared ones. */
  hasOwned: boolean;
}

function parseShelf(value: unknown): GadgetShelf {
  if (!isRecord(value) || !Array.isArray(value.pinned)) {
    throw malformed("gadget shelf");
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

// The most pins, and gadgets shown beside a session, one page holds.
const SHELF_PAGE_SIZE = 50;
const SESSION_GADGETS_PAGE_SIZE = 20;

/** The viewer's pinned gadgets, for the sidebar, every page of them. */
export async function getGadgetShelf(signal?: AbortSignal): Promise<GadgetShelf> {
  const { items, first } = await everyPage(`${GADGETS_PATH}/shelf`, {}, signal, "gadget shelf", {
    key: "pinned",
    limit: SHELF_PAGE_SIZE,
  });
  return parseShelf({ pinned: items, has_owned: first?.has_owned });
}

/** Pins or unpins a gadget for the viewer only. */
export async function pinGadget(appId: string, pinned: boolean): Promise<GadgetShelf> {
  const body = await gadgetRequest(appPath(appId, "/pin"), {
    method: "PUT",
    body: JSON.stringify({ pinned }),
  });
  // The answer is the shelf's first page; read the rest when there is more.
  if (isRecord(body) && typeof body.next_cursor === "string") return getGadgetShelf();
  return parseShelf(body);
}

function parseSessionGadgets(value: unknown): Gadget[] {
  if (!isRecord(value) || !Array.isArray(value.items)) {
    throw malformed("session gadgets");
  }
  return value.items.map(parseGadget);
}

/** The gadgets the viewer shows beside a session, in the order they added them. */
export async function getSessionGadgets(
  sessionId: string,
  signal?: AbortSignal,
): Promise<Gadget[]> {
  const { items } = await everyPage(
    `${GADGETS_PATH}/sessions/${encodeURIComponent(sessionId)}`,
    {},
    signal,
    "session gadgets",
    { limit: SESSION_GADGETS_PAGE_SIZE },
  );
  return items.map(parseGadget);
}

/**
 * Shows a gadget beside a session or stops showing it there. Showing it also sends the gadget's
 * actions from its own page to that session when the viewer can edit the gadget.
 */
export async function linkGadgetToSession(
  appId: string,
  sessionId: string,
  linked: boolean,
  { handlesActions = true }: { handlesActions?: boolean } = {},
): Promise<Gadget[]> {
  // Showing a gadget beside a session also sends it the gadget's actions, unless the session
  // is only one the gadget points to.
  const body = handlesActions ? { linked } : { linked, handles_actions: false };
  const answer = await gadgetRequest(appPath(appId, `/sessions/${encodeURIComponent(sessionId)}`), {
    method: "PUT",
    body: JSON.stringify(body),
  });
  // The answer is the first page of the session's gadgets; read the rest when there is more.
  if (isRecord(answer) && typeof answer.next_cursor === "string") {
    return getSessionGadgets(sessionId);
  }
  return parseSessionGadgets(answer);
}

/** Where a comment points in the gadget's page: the selected text and, if set, its record. */
export interface GadgetCommentAnchor {
  text: string;
  label?: string | null;
  ref?: string | null;
  occurrence?: number;
}

export interface GadgetComment {
  id: string;
  body: string;
  status: "draft" | "addressed";
  anchor: GadgetCommentAnchor;
  createdBy: string;
  /** Whether the viewer wrote it: their user and registration, which the server decides. */
  yours?: boolean;
  /** Unix seconds. */
  createdAt: number;
  updatedAt: number;
}

function parseComment(value: unknown): GadgetComment {
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
    yours: typeof value.yours === "boolean" ? value.yours : undefined,
    createdAt: typeof value.created_at === "number" ? value.created_at : 0,
    updatedAt: typeof value.updated_at === "number" ? value.updated_at : 0,
  };
}

export async function listGadgetComments(
  appId: string,
  signal?: AbortSignal,
): Promise<{ items: GadgetComment[]; canComment: boolean }> {
  const { items, first } = await everyPage(appPath(appId, "/comments"), {}, signal, "comment list");
  return {
    items: items
      .map(parseComment)
      .sort((a, b) => a.createdAt - b.createdAt || a.id.localeCompare(b.id)),
    canComment: first?.can_comment === true,
  };
}

export async function createGadgetComment(
  appId: string,
  { body, anchor, operationId }: { body: string; anchor: GadgetCommentAnchor; operationId: string },
): Promise<GadgetComment> {
  return parseComment(
    await gadgetRequest(appPath(appId, "/comments"), {
      method: "POST",
      body: JSON.stringify({ body, anchor, operation_id: operationId }),
    }),
  );
}

export async function updateGadgetComment(
  appId: string,
  commentId: string,
  change: { body?: string; status?: "draft" | "addressed"; ifBody?: string },
): Promise<GadgetComment> {
  const { ifBody, ...rest } = change;
  return parseComment(
    await gadgetRequest(appPath(appId, `/comments/${encodeURIComponent(commentId)}`), {
      method: "PATCH",
      body: JSON.stringify(ifBody === undefined ? rest : { ...rest, if_body: ifBody }),
    }),
  );
}

export async function deleteGadgetComment(appId: string, commentId: string): Promise<void> {
  await gadgetRequest(appPath(appId, `/comments/${encodeURIComponent(commentId)}`), {
    method: "DELETE",
  });
}

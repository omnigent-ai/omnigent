// Wire protocol between an uploaded thingy frame and the Omnigent web app.
// Framework-independent: no React or app imports, so the frame runtime, the
// host bridge and their tests share one definition of the envelope and its budgets.

export const THINGY_PROTOCOL_VERSION = 1;
export const THINGY_SDK_VERSION = 1;

/** Window messages exchanged before the per-mount MessagePort exists. */
export const THINGY_HELLO_MESSAGE_TYPE = "omnigentThingy:hello";
export const THINGY_CONNECT_MESSAGE_TYPE = "omnigentThingy:connect";

/**
 * Budgets enforced by both sides. The server is the authorization boundary; these only
 * keep a misbehaving frame from flooding the host or the RPC route.
 */
export const THINGY_LIMITS = {
  /** Serialized size of one port message, request or response (matches the RPC route's 64 KiB budget). */
  maxMessageBytes: 64 * 1024,
  /** Requests a frame may have outstanding at once. */
  maxInFlightRequests: 8,
  /** Requests a frame may start per rolling minute. */
  maxRequestsPerMinute: 240,
  /** Malformed or out-of-budget messages tolerated before the host closes the port. */
  maxProtocolViolations: 20,
  /** Times one mount may perform the hello handshake (the frame can reload itself). */
  maxHandshakesPerMount: 3,
  defaultDeadlineMs: 15_000,
  maxDeadlineMs: 60_000,
  minDeadlineMs: 100,
  /** Ready handshake budget for `connect()`. */
  connectTimeoutMs: 10_000,
  minWatchIntervalMs: 1_000,
  maxWatchIntervalMs: 5 * 60_000,
  maxWatchBackoffMs: 30_000,
  /** Consecutive failed polls after which a watch reports `stale` instead of `reconnecting`. */
  watchStaleAfterFailures: 3,
  maxListLimit: 100,
  maxIdentifierLength: 64,
  /** Characters an app may put in a New session prompt; the prompt travels in the page URL. */
  maxSessionPromptLength: 6000,
  maxOperationIdLength: 128,
  maxJsonDepth: 16,
  maxJsonNodes: 16_384,
} as const;

export type ThingyCapability = "data.read" | "data.write" | "sessions.read" | "sessions.create";

export const THINGY_DATA_METHODS = [
  "data.v1.get",
  "data.v1.list",
  "data.v1.create",
  "data.v1.replace",
  "data.v1.delete",
] as const;
export const THINGY_SESSION_METHODS = ["sessions.v1.list", "sessions.v1.create"] as const;
/**
 * Open an Omnigent page. They need no capability: the page is one the viewer could open by
 * URL, nothing starts until the viewer acts on it, and the host opens it only right after a
 * click inside the app.
 */
export const THINGY_NAVIGATION_METHODS = [
  "navigation.v1.newSession",
  "navigation.v1.openSession",
  "navigation.v1.newAutomation",
] as const;

/**
 * Where a navigation call opens its page:
 * - `tab`: a new browser tab (the default).
 * - `panel`: a pane beside the thingy, which keeps running. New session starts there and the
 *   pane then shows the session. Sessions only; automations open in a tab or the page.
 * - `page`: this tab, in place of the thingy. The browser's Back button returns to it.
 */
export const THINGY_NAVIGATION_DISPLAYS = ["tab", "panel", "page"] as const;
export type ThingyNavigationDisplay = (typeof THINGY_NAVIGATION_DISPLAYS)[number];

/**
 * `agent.v1.request` asks the viewer's thingy session to carry out one of the thingy's declared
 * actions. It needs no capability but, like navigation, a click inside the app, and the viewer
 * must be able to edit the thingy. `agent.v1.progress` reads how far actions have got.
 */
export const THINGY_AGENT_METHODS = ["agent.v1.request", "agent.v1.progress"] as const;

/**
 * `ui.v1.selection` reports what the viewer selected in the thingy (or `null` once cleared), so
 * Omnigent can offer Reply and Ask in side chat for it, as in a session. The SDK sends it by
 * itself; thingies do not call it.
 */
export const THINGY_UI_METHODS = ["ui.v1.selection"] as const;

export type ThingyDataMethod = (typeof THINGY_DATA_METHODS)[number];
export type ThingySessionMethod = (typeof THINGY_SESSION_METHODS)[number];
export type ThingyNavigationMethod = (typeof THINGY_NAVIGATION_METHODS)[number];
export type ThingyAgentMethod = (typeof THINGY_AGENT_METHODS)[number];
export type ThingyUiMethod = (typeof THINGY_UI_METHODS)[number];
export type ThingyMethod =
  | ThingyDataMethod
  | ThingySessionMethod
  | ThingyNavigationMethod
  | ThingyAgentMethod
  | ThingyUiMethod;

/** The capability a published revision must hold for each method; `null` when none is needed. */
export const THINGY_METHOD_CAPABILITY: Record<ThingyMethod, ThingyCapability | null> = {
  "data.v1.get": "data.read",
  "data.v1.list": "data.read",
  "data.v1.create": "data.write",
  "data.v1.replace": "data.write",
  "data.v1.delete": "data.write",
  "sessions.v1.list": "sessions.read",
  "sessions.v1.create": "sessions.create",
  "navigation.v1.newSession": null,
  "navigation.v1.openSession": null,
  "navigation.v1.newAutomation": null,
  "agent.v1.request": null,
  "agent.v1.progress": null,
  "ui.v1.selection": null,
};

/** Methods with side effects. They are never retried automatically. */
export const THINGY_MUTATING_METHODS: ReadonlySet<ThingyMethod> = new Set<ThingyMethod>([
  "data.v1.create",
  "data.v1.replace",
  "data.v1.delete",
  "sessions.v1.create",
  "navigation.v1.newSession",
  "navigation.v1.openSession",
  "navigation.v1.newAutomation",
  "agent.v1.request",
]);

export type ThingyErrorCode =
  | "INVALID_ARGUMENT"
  | "NOT_FOUND"
  | "CONFLICT"
  | "PERMISSION_DENIED"
  | "UNAUTHENTICATED"
  /** The method or capability is not implemented or not advertised for this revision. */
  | "UNSUPPORTED"
  /** Transient transport or server failure; a read may be retried. */
  | "UNAVAILABLE"
  | "DEADLINE_EXCEEDED"
  | "CANCELLED"
  | "RESOURCE_EXHAUSTED"
  /** The published revision changed; reload to run the new one. */
  | "REVISION_CHANGED"
  /** Stored state failed validation on the server. Not a version conflict; retrying will not help. */
  | "DATA_LOSS"
  /** The write would break one of the thingy's rules; the message says which. Retrying will not help. */
  | "CONSTRAINT_VIOLATION"
  | "DISPOSED"
  | "INTERNAL";

export const THINGY_ERROR_CODES: ReadonlySet<string> = new Set<ThingyErrorCode>([
  "INVALID_ARGUMENT",
  "NOT_FOUND",
  "CONFLICT",
  "PERMISSION_DENIED",
  "UNAUTHENTICATED",
  "UNSUPPORTED",
  "UNAVAILABLE",
  "DEADLINE_EXCEEDED",
  "CANCELLED",
  "RESOURCE_EXHAUSTED",
  "REVISION_CHANGED",
  "DATA_LOSS",
  "CONSTRAINT_VIOLATION",
  "DISPOSED",
  "INTERNAL",
]);

/**
 * Codes as they travel to the frame. A well-formed code the web app does not know yet is
 * passed through unchanged so newer servers can add codes without being misreported.
 */
export type ThingyWireErrorCode = ThingyErrorCode | (string & Record<never, never>);

const WIRE_ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/;

export function isWellFormedErrorCode(value: unknown): value is string {
  return typeof value === "string" && WIRE_ERROR_CODE_PATTERN.test(value);
}

export interface ThingyWireError {
  code: ThingyWireErrorCode;
  message: string;
}

/** Context the host sends once per port. Contains no credentials, URLs, or routing parameters. */
export interface ThingyInitContext {
  protocol: typeof THINGY_PROTOCOL_VERSION;
  app: { id: string; name: string };
  revisionId: string;
  /** The signed-in viewer (not the app owner): a stable opaque ID plus a name to show. */
  viewer: { id: string; displayName: string };
  /** Capabilities granted to this viewer for this revision. */
  capabilities: ThingyCapability[];
  permissions: { writeData: boolean; publish: boolean };
  colorScheme: "light" | "dark";
}

export interface ThingyInitMessage {
  v: typeof THINGY_PROTOCOL_VERSION;
  kind: "init";
  context: ThingyInitContext;
}

export interface ThingyRequestMessage {
  v: typeof THINGY_PROTOCOL_VERSION;
  kind: "request";
  id: string;
  method: ThingyMethod;
  params: Record<string, unknown>;
  deadlineMs: number;
}

export interface ThingyCancelMessage {
  v: typeof THINGY_PROTOCOL_VERSION;
  kind: "cancel";
  id: string;
}

export type ThingyResponseMessage =
  | { v: typeof THINGY_PROTOCOL_VERSION; kind: "response"; id: string; ok: true; result: unknown }
  | {
      v: typeof THINGY_PROTOCOL_VERSION;
      kind: "response";
      id: string;
      ok: false;
      error: ThingyWireError;
    };

export type ThingyFrameMessage = ThingyRequestMessage | ThingyCancelMessage;
export type ThingyHostMessage = ThingyInitMessage | ThingyResponseMessage;

const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/** One of the viewer's own sessions, as a thingy with `sessions.read` sees it. */
export interface ThingyViewerSession {
  id: string;
  title: string;
  status: string;
  harness: string | null;
  agentName: string | null;
  createdAt: number | null;
  updatedAt: number | null;
  unread: boolean;
  needsInput: boolean;
  backgroundWork: boolean;
}

export interface ThingySessionListRequest {
  limit: number;
  after?: string;
}

export function isThingyNavigationMethod(value: unknown): value is ThingyNavigationMethod {
  return (
    typeof value === "string" && (THINGY_NAVIGATION_METHODS as readonly string[]).includes(value)
  );
}

export function isThingyMethod(value: unknown): value is ThingyMethod {
  return typeof value === "string" && Object.hasOwn(THINGY_METHOD_CAPABILITY, value);
}

/** Reject values JSON.stringify would silently change, including non-finite numbers and Dates. */
export function isJsonDocumentValue(value: unknown): boolean {
  let remaining = THINGY_LIMITS.maxJsonNodes;
  function visit(entry: unknown, depth: number): boolean {
    remaining -= 1;
    if (remaining < 0 || depth > THINGY_LIMITS.maxJsonDepth) return false;
    if (entry === null || typeof entry === "string" || typeof entry === "boolean") return true;
    if (typeof entry === "number") {
      return Number.isFinite(entry) && (!Number.isInteger(entry) || Number.isSafeInteger(entry));
    }
    if (Array.isArray(entry)) {
      for (const item of entry) {
        if (!visit(item, depth + 1)) return false;
      }
      return true;
    }
    if (!isPlainObject(entry)) return false;
    for (const item of Object.values(entry)) {
      if (!visit(item, depth + 1)) return false;
    }
    return true;
  }
  try {
    return visit(value, 0);
  } catch {
    return false;
  }
}

/** UTF-8 size of the JSON encoding, or undefined when the value is not JSON-serializable. */
export function serializedByteLength(value: unknown): number | undefined {
  let json: string | undefined;
  try {
    json = JSON.stringify(value);
  } catch {
    return undefined;
  }
  if (json === undefined) {
    return undefined;
  }
  return new TextEncoder().encode(json).length;
}

export type FrameMessageParseResult =
  { ok: true; message: ThingyFrameMessage } | { ok: false; reason: string; requestId?: string };

/** Strictly validates a message received on the host side of the port. */
export function parseFrameMessage(data: unknown): FrameMessageParseResult {
  if (!isPlainObject(data)) {
    return { ok: false, reason: "message is not an object" };
  }
  const requestId =
    typeof data.id === "string" && REQUEST_ID_PATTERN.test(data.id) ? data.id : undefined;
  if (data.v !== THINGY_PROTOCOL_VERSION) {
    return { ok: false, reason: "unsupported protocol version", requestId };
  }
  const size = serializedByteLength(data);
  if (size === undefined || size > THINGY_LIMITS.maxMessageBytes) {
    return { ok: false, reason: "message exceeds the size budget", requestId };
  }
  if (requestId === undefined) {
    return { ok: false, reason: "missing or invalid request id" };
  }
  if (data.kind === "cancel") {
    const allowed = ["v", "kind", "id"];
    if (Object.keys(data).some((key) => !allowed.includes(key))) {
      return { ok: false, reason: "unexpected cancel fields", requestId };
    }
    return { ok: true, message: { v: THINGY_PROTOCOL_VERSION, kind: "cancel", id: requestId } };
  }
  if (data.kind !== "request") {
    return { ok: false, reason: "unknown message kind", requestId };
  }
  const allowed = ["v", "kind", "id", "method", "params", "deadlineMs"];
  if (Object.keys(data).some((key) => !allowed.includes(key))) {
    return { ok: false, reason: "unexpected request fields", requestId };
  }
  if (!isThingyMethod(data.method)) {
    return { ok: false, reason: "unknown method", requestId };
  }
  if (!isPlainObject(data.params)) {
    return { ok: false, reason: "params must be an object", requestId };
  }
  if (
    (data.method === "data.v1.create" || data.method === "data.v1.replace") &&
    !isJsonDocumentValue(data.params.value)
  ) {
    return { ok: false, reason: "document value is not supported JSON", requestId };
  }
  const deadlineMs =
    typeof data.deadlineMs === "number" && Number.isFinite(data.deadlineMs)
      ? Math.min(
          Math.max(data.deadlineMs, THINGY_LIMITS.minDeadlineMs),
          THINGY_LIMITS.maxDeadlineMs,
        )
      : THINGY_LIMITS.defaultDeadlineMs;
  return {
    ok: true,
    message: {
      v: THINGY_PROTOCOL_VERSION,
      kind: "request",
      id: requestId,
      method: data.method,
      params: data.params,
      deadlineMs,
    },
  };
}

export function isThingyErrorCode(value: unknown): value is ThingyErrorCode {
  return typeof value === "string" && THINGY_ERROR_CODES.has(value);
}

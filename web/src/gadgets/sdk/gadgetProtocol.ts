// Wire protocol between an uploaded gadget frame and the Omnigent web app.
// Framework-independent: no React or app imports, so the frame runtime, the
// host bridge and their tests share one definition of the envelope and its budgets.

export const GADGET_PROTOCOL_VERSION = 1;
export const GADGET_SDK_VERSION = 1;

/** Window messages exchanged before the per-mount MessagePort exists. */
export const GADGET_HELLO_MESSAGE_TYPE = "omnigentGadget:hello";
export const GADGET_CONNECT_MESSAGE_TYPE = "omnigentGadget:connect";

/**
 * Budgets enforced by both sides. The server is the authorization boundary; these only
 * keep a misbehaving frame from flooding the host or the RPC route.
 */
export const GADGET_LIMITS = {
  /** Serialized size of one port message, request or response (matches the RPC route's 64 KiB budget). */
  maxMessageBytes: 64 * 1024,
  /** Requests a frame may have outstanding at once. */
  maxInFlightRequests: 8,
  /** Requests a frame may start per rolling minute. */
  maxRequestsPerMinute: 240,
  /** Malformed or out-of-budget messages tolerated before the host closes the port. */
  maxProtocolViolations: 20,
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

export type GadgetCapability = "data.read" | "data.write" | "sessions.read";

export const GADGET_DATA_METHODS = [
  "data.v1.get",
  "data.v1.list",
  "data.v1.create",
  "data.v1.replace",
  "data.v1.delete",
] as const;
export const GADGET_SESSION_METHODS = ["sessions.v1.list"] as const;
/**
 * Open an Omnigent page. They need no capability: the page is one the viewer could open by
 * URL, nothing starts until the viewer acts on it, and the host opens it only right after a
 * click inside the app.
 */
export const GADGET_NAVIGATION_METHODS = [
  "navigation.v1.newSession",
  "navigation.v1.openSession",
  "navigation.v1.newAutomation",
] as const;

/**
 * Where a navigation call opens its page:
 * - `tab`: a new browser tab (the default).
 * - `panel`: a pane beside the gadget, which keeps running. New session starts there and the
 *   pane then shows the session. Sessions only; automations open in a tab or the page.
 * - `page`: this tab, in place of the gadget. The browser's Back button returns to it.
 */
export const GADGET_NAVIGATION_DISPLAYS = ["tab", "panel", "page"] as const;
export type GadgetNavigationDisplay = (typeof GADGET_NAVIGATION_DISPLAYS)[number];

/**
 * `agent.v1.request` asks the viewer's gadget session to carry out one of the gadget's declared
 * actions. It needs no capability but, like navigation, a click inside the app, and the viewer
 * must be able to edit the gadget. `agent.v1.progress` reads how far actions have got.
 */
export const GADGET_AGENT_METHODS = ["agent.v1.request", "agent.v1.progress"] as const;

/**
 * `ui.v1.selection` reports what the viewer selected in the gadget (or `null` once cleared), so
 * Omnigent can offer Reply and Ask in side chat for it, as in a session. The SDK sends it by
 * itself; gadgets do not call it.
 */
export const GADGET_UI_METHODS = ["ui.v1.selection"] as const;

export type GadgetDataMethod = (typeof GADGET_DATA_METHODS)[number];
export type GadgetSessionMethod = (typeof GADGET_SESSION_METHODS)[number];
export type GadgetNavigationMethod = (typeof GADGET_NAVIGATION_METHODS)[number];
export type GadgetAgentMethod = (typeof GADGET_AGENT_METHODS)[number];
export type GadgetUiMethod = (typeof GADGET_UI_METHODS)[number];
export type GadgetMethod =
  | GadgetDataMethod
  | GadgetSessionMethod
  | GadgetNavigationMethod
  | GadgetAgentMethod
  | GadgetUiMethod;

/** The capability a published revision must hold for each method; `null` when none is needed. */
export const GADGET_METHOD_CAPABILITY: Record<GadgetMethod, GadgetCapability | null> = {
  "data.v1.get": "data.read",
  "data.v1.list": "data.read",
  "data.v1.create": "data.write",
  "data.v1.replace": "data.write",
  "data.v1.delete": "data.write",
  "sessions.v1.list": "sessions.read",
  "navigation.v1.newSession": null,
  "navigation.v1.openSession": null,
  "navigation.v1.newAutomation": null,
  "agent.v1.request": null,
  "agent.v1.progress": null,
  "ui.v1.selection": null,
};

/** Methods with side effects. They are never retried automatically. */
export const GADGET_MUTATING_METHODS: ReadonlySet<GadgetMethod> = new Set<GadgetMethod>([
  "data.v1.create",
  "data.v1.replace",
  "data.v1.delete",
  "navigation.v1.newSession",
  "navigation.v1.openSession",
  "navigation.v1.newAutomation",
  "agent.v1.request",
]);

export type GadgetErrorCode =
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
  /** The write would break one of the gadget's rules; the message says which. Retrying will not help. */
  | "CONSTRAINT_VIOLATION"
  | "DISPOSED"
  | "INTERNAL";

export const GADGET_ERROR_CODES: ReadonlySet<string> = new Set<GadgetErrorCode>([
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
export type GadgetWireErrorCode = GadgetErrorCode | (string & Record<never, never>);

const WIRE_ERROR_CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/;

export function isWellFormedErrorCode(value: unknown): value is string {
  return typeof value === "string" && WIRE_ERROR_CODE_PATTERN.test(value);
}

export interface GadgetWireError {
  code: GadgetWireErrorCode;
  message: string;
  /** The server's request ID, for support; not part of the message a gadget shows. */
  requestId?: string;
}

/** Context the host sends once per port. Contains no credentials, URLs, or routing parameters. */
export interface GadgetInitContext {
  protocol: typeof GADGET_PROTOCOL_VERSION;
  app: { id: string; name: string };
  revisionId: string;
  /**
   * The signed-in viewer (not the app owner): an opaque ID stable within this gadget (records
   * name their authors the same way), and a real name to show or `null`; never a login or email.
   */
  viewer: { id: string; displayName: string | null };
  /** Capabilities granted to this viewer for this revision. */
  capabilities: GadgetCapability[];
  permissions: { writeData: boolean; publish: boolean };
  colorScheme: "light" | "dark";
  /** A template preview: data lives in memory and actions are off. Older hosts omit it. */
  preview?: boolean;
}

export interface GadgetInitMessage {
  v: typeof GADGET_PROTOCOL_VERSION;
  kind: "init";
  context: GadgetInitContext;
}

export interface GadgetRequestMessage {
  v: typeof GADGET_PROTOCOL_VERSION;
  kind: "request";
  id: string;
  method: GadgetMethod;
  params: Record<string, unknown>;
  deadlineMs: number;
}

export interface GadgetCancelMessage {
  v: typeof GADGET_PROTOCOL_VERSION;
  kind: "cancel";
  id: string;
}

export type GadgetResponseMessage =
  | { v: typeof GADGET_PROTOCOL_VERSION; kind: "response"; id: string; ok: true; result: unknown }
  | {
      v: typeof GADGET_PROTOCOL_VERSION;
      kind: "response";
      id: string;
      ok: false;
      error: GadgetWireError;
    };

/**
 * Tells the frame whether the viewer's gadget session is being created: from their Create session
 * until that session has the requests made before it. Requests wait for it meanwhile.
 */
export interface GadgetAgentStartingMessage {
  v: typeof GADGET_PROTOCOL_VERSION;
  kind: "event";
  event: "agent.starting";
  starting: boolean;
}

export type GadgetFrameMessage = GadgetRequestMessage | GadgetCancelMessage;
export type GadgetHostMessage =
  GadgetInitMessage | GadgetResponseMessage | GadgetAgentStartingMessage;

const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

/** One of the viewer's own sessions, as a gadget with `sessions.read` sees it. */
export interface GadgetViewerSession {
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

export interface GadgetSessionListRequest {
  limit: number;
  after?: string;
}

export function isGadgetNavigationMethod(value: unknown): value is GadgetNavigationMethod {
  return (
    typeof value === "string" && (GADGET_NAVIGATION_METHODS as readonly string[]).includes(value)
  );
}

export function isGadgetMethod(value: unknown): value is GadgetMethod {
  return typeof value === "string" && Object.hasOwn(GADGET_METHOD_CAPABILITY, value);
}

/** Reject values JSON.stringify would silently change, including non-finite numbers and Dates. */
export function isJsonDocumentValue(value: unknown): boolean {
  let remaining = GADGET_LIMITS.maxJsonNodes;
  function visit(entry: unknown, depth: number): boolean {
    remaining -= 1;
    if (remaining < 0 || depth > GADGET_LIMITS.maxJsonDepth) return false;
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

/** Levels a request envelope may add around a document of `maxJsonDepth`. */
const ENVELOPE_DEPTH = 8;

/**
 * Whether `value` is plain JSON whose encoding could fit in `maxBytes`, checked without
 * serializing it. Every value counts each time it is reached, so a shared reference costs as
 * much as its copies would, and each node, string and key adds to a lower bound on the encoded
 * size: a small message whose repeated references would expand when serialized stops here
 * after at most about `maxBytes` steps, before `JSON.stringify` builds anything.
 */
function fitsMessageBudget(value: unknown, maxBytes: number): boolean {
  let bytes = 0;
  function visit(entry: unknown, depth: number): boolean {
    bytes += 1;
    if (bytes > maxBytes || depth > GADGET_LIMITS.maxJsonDepth + ENVELOPE_DEPTH) return false;
    if (entry === null || typeof entry === "number" || typeof entry === "boolean") return true;
    if (typeof entry === "string") {
      // A UTF-8 encoding is never shorter than the string's UTF-16 length.
      bytes += entry.length;
      return bytes <= maxBytes;
    }
    if (Array.isArray(entry)) {
      for (let index = 0; index < entry.length; index += 1) {
        if (!visit(entry[index], depth + 1)) return false;
      }
      return true;
    }
    if (!isPlainObject(entry)) return false;
    for (const key in entry) {
      if (!Object.hasOwn(entry, key)) continue;
      bytes += key.length + 1;
      if (!visit(entry[key], depth + 1)) return false;
    }
    return true;
  }
  return visit(value, 0);
}

export type FrameMessageParseResult =
  { ok: true; message: GadgetFrameMessage } | { ok: false; reason: string; requestId?: string };

/** Strictly validates a message received on the host side of the port. */
export function parseFrameMessage(data: unknown): FrameMessageParseResult {
  if (!isPlainObject(data)) {
    return { ok: false, reason: "message is not an object" };
  }
  const requestId =
    typeof data.id === "string" && REQUEST_ID_PATTERN.test(data.id) ? data.id : undefined;
  if (data.v !== GADGET_PROTOCOL_VERSION) {
    return { ok: false, reason: "unsupported protocol version", requestId };
  }
  // A message from the frame is untrusted: bound the work before serializing it.
  const size = fitsMessageBudget(data, GADGET_LIMITS.maxMessageBytes)
    ? serializedByteLength(data)
    : undefined;
  if (size === undefined || size > GADGET_LIMITS.maxMessageBytes) {
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
    return { ok: true, message: { v: GADGET_PROTOCOL_VERSION, kind: "cancel", id: requestId } };
  }
  if (data.kind !== "request") {
    return { ok: false, reason: "unknown message kind", requestId };
  }
  const allowed = ["v", "kind", "id", "method", "params", "deadlineMs"];
  if (Object.keys(data).some((key) => !allowed.includes(key))) {
    return { ok: false, reason: "unexpected request fields", requestId };
  }
  if (!isGadgetMethod(data.method)) {
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
          Math.max(data.deadlineMs, GADGET_LIMITS.minDeadlineMs),
          GADGET_LIMITS.maxDeadlineMs,
        )
      : GADGET_LIMITS.defaultDeadlineMs;
  return {
    ok: true,
    message: {
      v: GADGET_PROTOCOL_VERSION,
      kind: "request",
      id: requestId,
      method: data.method,
      params: data.params,
      deadlineMs,
    },
  };
}

export function isGadgetErrorCode(value: unknown): value is GadgetErrorCode {
  return typeof value === "string" && GADGET_ERROR_CODES.has(value);
}

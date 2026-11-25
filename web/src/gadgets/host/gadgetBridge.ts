// Host end of one mount's MessagePort. Validates every frame message, enforces the
// per-mount budgets, translates the SDK's camelCase calls into the RPC route's snake_case
// shape, and maps results and errors back. The server stays the authorization boundary:
// this only refuses what it can refuse early and never widens what the server allows.

import { GadgetApiError } from "../api/gadgetApi";
import type {
  GadgetCapability,
  GadgetErrorCode,
  GadgetInitContext,
  GadgetMethod,
  GadgetNavigationDisplay,
  GadgetNavigationMethod,
  GadgetRequestMessage,
  GadgetSessionListRequest,
  GadgetViewerSession,
  GadgetResponseMessage,
  GadgetWireError,
} from "../sdk/gadgetProtocol";
import {
  GADGET_LIMITS,
  GADGET_METHOD_CAPABILITY,
  GADGET_NAVIGATION_DISPLAYS,
  GADGET_PROTOCOL_VERSION,
  isJsonDocumentValue,
  isGadgetNavigationMethod,
  isWellFormedErrorCode,
  parseFrameMessage,
  serializedByteLength,
} from "../sdk/gadgetProtocol";

export interface GadgetPort {
  postMessage: (message: unknown) => void;
  addEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
  removeEventListener: (type: "message", listener: (event: MessageEvent) => void) => void;
  start?: () => void;
  close: () => void;
}

export type GadgetBridgeCloseReason = "disposed" | "protocol_violations" | "revision_changed";

/** An action of the gadget the viewer asks their gadget session to carry out. */
export interface GadgetActionRequest {
  action: string;
  params: Record<string, unknown>;
  /** The call's idempotency key: asking again with it answers from the first request. */
  operationId?: string;
}

/** An Omnigent page a navigation call asks the host to open. */
export type GadgetPageRequest =
  | { kind: "newSession"; prompt: string; display: GadgetNavigationDisplay }
  | {
      kind: "openSession";
      sessionId: string;
      display: GadgetNavigationDisplay;
    }
  | {
      kind: "newAutomation";
      name: string;
      prompt: string;
      schedule?: string;
      display: "tab" | "page";
    };

export interface StartGadgetBridgeOptions {
  port: GadgetPort;
  context: GadgetInitContext;
  /** Sends one call to the RPC route. `params` are already in the route's snake_case shape. */
  callRpc: (
    method: GadgetMethod,
    params: Record<string, unknown>,
    signal: AbortSignal,
  ) => Promise<unknown>;
  /**
   * Opens the page for a navigation call. Returns false when it did not, for example because
   * the viewer did not just click inside the app. Without it, navigation calls are refused.
   */
  openPage?: (request: GadgetPageRequest) => boolean;
  /**
   * Hands an `agent.v1.request` to the viewer's gadget session and resolves with what the frame
   * receives. Throw a `BridgeError` to refuse it. Without it, action requests are refused.
   */
  /** `signal` aborts when the frame cancels the call, so work not yet sent can stop. */
  requestAction?: (
    request: GadgetActionRequest,
    signal: AbortSignal,
  ) => Promise<Record<string, unknown>>;
  /** Receives what the viewer selected in the gadget, or null once the selection is gone. */
  onSelection?: (selection: GadgetSelection | null) => void;
  /** Reads action progress for `agent.v1.progress`: the given IDs, or the viewer's tracked actions. */
  actionProgress?: (actionIds: string[], signal: AbortSignal) => Promise<Record<string, unknown>[]>;
  /**
   * Lists the viewer's own sessions for `sessions.v1.list`, after asking the viewer; resolves
   * `null` when the viewer does not allow it. Without it, the call goes to the RPC route.
   */
  listSessions?: (
    request: GadgetSessionListRequest,
    signal: AbortSignal,
  ) => Promise<{
    items: GadgetViewerSession[];
    nextCursor: string | null;
  } | null>;
  onRevisionChanged?: () => void;
  onClosed?: (reason: GadgetBridgeCloseReason) => void;
  now?: () => number;
}

export interface GadgetBridge {
  dispose: () => void;
  /** Tells the frame whether the viewer's gadget session is being created now. */
  setAgentStarting: (starting: boolean) => void;
}

const MAX_ERROR_MESSAGE_LENGTH = 500;

/** A failure the frame sees with this code and message. */
export class BridgeError extends Error {
  readonly code: GadgetErrorCode;
  constructor(code: GadgetErrorCode, message: string) {
    super(message);
    this.code = code;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireKeys(
  params: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): void {
  for (const key of Object.keys(params)) {
    if (!required.includes(key) && !optional.includes(key)) {
      throw new BridgeError("INVALID_ARGUMENT", `Unexpected parameter "${key.slice(0, 64)}"`);
    }
  }
  for (const key of required) {
    if (!(key in params)) {
      throw new BridgeError("INVALID_ARGUMENT", `Missing parameter "${key}"`);
    }
  }
}

function requireString(params: Record<string, unknown>, key: string, maxLength: number): string {
  const value = params[key];
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    throw new BridgeError(
      "INVALID_ARGUMENT",
      `"${key}" must be a non-empty string of at most ${maxLength} characters`,
    );
  }
  return value;
}

function camelToSnake(key: string): string {
  return key.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`);
}

function snakeToCamelKeys(value: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value)) {
    result[key.replace(/_([a-z])/g, (_match, letter: string) => letter.toUpperCase())] = entry;
  }
  return result;
}

const SESSION_PARAM_KEY = /^[A-Za-z][A-Za-z0-9]{0,63}$/;
const SESSION_ID = /^[A-Za-z0-9_-]{1,128}$/;
// An RRULE as Automations stores it, such as `FREQ=DAILY;BYHOUR=9;BYMINUTE=0`.
const RRULE = /^[A-Z]+=[A-Za-z0-9,+-]+(;[A-Z]+=[A-Za-z0-9,+-]+)*$/;

/** Validates a `sessions.v1.list` call answered by the host. */
export function toSessionListRequest(params: Record<string, unknown>): GadgetSessionListRequest {
  requireKeys(params, [], ["limit", "after"]);
  const limit = params.limit ?? 50;
  if (
    typeof limit !== "number" ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > GADGET_LIMITS.maxListLimit
  ) {
    throw new BridgeError(
      "INVALID_ARGUMENT",
      `"limit" must be an integer from 1 to ${GADGET_LIMITS.maxListLimit}`,
    );
  }
  return params.after === undefined
    ? { limit }
    : { limit, after: requireString(params, "after", 128) };
}

function requireDisplay<T extends GadgetNavigationDisplay>(
  params: Record<string, unknown>,
  allowed: readonly T[],
): T {
  const display = params.display ?? "tab";
  if (!allowed.includes(display as T)) {
    throw new BridgeError("INVALID_ARGUMENT", `"display" must be one of ${allowed.join(", ")}`);
  }
  return display as T;
}

const ACTION_NAME = /^[a-z0-9][a-z0-9_-]{0,63}$/;
// The server's operation ID pattern.
const OPERATION_ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/;
const MAX_ACTION_PARAMS_BYTES = 8 * 1024;

/** Validates an `agent.v1.request` call; the server checks it against the gadget's declared actions. */
export function toActionRequest(params: Record<string, unknown>): GadgetActionRequest {
  requireKeys(params, ["action"], ["params", "operationId"]);
  const action = params.action;
  if (typeof action !== "string" || !ACTION_NAME.test(action)) {
    throw new BridgeError(
      "INVALID_ARGUMENT",
      '"action" must be the name of one of the gadget\'s actions',
    );
  }
  const actionParams = params.params ?? {};
  if (!isRecord(actionParams) || !isJsonDocumentValue(actionParams)) {
    throw new BridgeError("INVALID_ARGUMENT", '"params" must be a JSON object');
  }
  if ((serializedByteLength(actionParams) ?? Infinity) > MAX_ACTION_PARAMS_BYTES) {
    throw new BridgeError(
      "INVALID_ARGUMENT",
      `"params" must be at most ${MAX_ACTION_PARAMS_BYTES / 1024} KiB`,
    );
  }
  if (params.operationId === undefined) return { action, params: actionParams };
  const operationId = requireString(params, "operationId", GADGET_LIMITS.maxOperationIdLength);
  if (!OPERATION_ID.test(operationId)) {
    throw new BridgeError(
      "INVALID_ARGUMENT",
      '"operationId" must be letters, digits and _ . : - starting with a letter or digit',
    );
  }
  return { action, params: actionParams, operationId };
}

/** What the viewer selected in a gadget; `rect` is in the frame's viewport. */
export interface GadgetSelection {
  text: string;
  rect: { x: number; y: number; width: number; height: number };
  ref?: string;
  label?: string;
  occurrence: number;
}

function finiteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Validates a `ui.v1.selection` report. */
export function toSelection(params: Record<string, unknown>): GadgetSelection | null {
  requireKeys(params, ["selection"], []);
  const selection = params.selection;
  if (selection === null) {
    return null;
  }
  if (!isRecord(selection)) {
    throw new BridgeError("INVALID_ARGUMENT", '"selection" must be an object or null');
  }
  requireKeys(selection, ["text", "rect", "occurrence"], ["ref", "label"]);
  const { text, rect, occurrence, ref, label } = selection;
  const validText = typeof text === "string" && text.trim() !== "" && text.length <= 4000;
  const validRect =
    isRecord(rect) &&
    finiteNumber(rect.x) &&
    finiteNumber(rect.y) &&
    finiteNumber(rect.width) &&
    finiteNumber(rect.height);
  const validOccurrence =
    Number.isInteger(occurrence) &&
    (occurrence as number) >= 1 &&
    (occurrence as number) <= 100_000;
  const validRef = ref === undefined || (typeof ref === "string" && ref.length <= 200);
  const validLabel = label === undefined || (typeof label === "string" && label.length <= 200);
  if (!validText || !validRect || !validOccurrence || !validRef || !validLabel) {
    throw new BridgeError("INVALID_ARGUMENT", "Invalid selection");
  }
  return {
    text: text as string,
    rect: {
      x: rect.x as number,
      y: rect.y as number,
      width: rect.width as number,
      height: rect.height as number,
    },
    occurrence: occurrence as number,
    ...(ref === undefined ? {} : { ref: ref as string }),
    ...(label === undefined ? {} : { label: label as string }),
  };
}

const MAX_PROGRESS_IDS = 20;
const ACTION_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;

/** Validates an `agent.v1.progress` call: up to 20 action IDs, or none for the viewer's tracked actions. */
export function toActionProgressRequest(params: Record<string, unknown>): string[] {
  requireKeys(params, [], ["actionIds"]);
  const ids = params.actionIds ?? [];
  if (
    !Array.isArray(ids) ||
    ids.length > MAX_PROGRESS_IDS ||
    !ids.every((id) => typeof id === "string" && ACTION_ID.test(id))
  ) {
    throw new BridgeError(
      "INVALID_ARGUMENT",
      `"actionIds" must be at most ${MAX_PROGRESS_IDS} action IDs`,
    );
  }
  return ids as string[];
}

/** Validates a navigation call's parameters. */
export function toPageRequest(
  method: GadgetNavigationMethod,
  params: Record<string, unknown>,
): GadgetPageRequest {
  if (method === "navigation.v1.newSession") {
    requireKeys(params, [], ["prompt", "display"]);
    const prompt =
      params.prompt === undefined || params.prompt === ""
        ? ""
        : requireString(params, "prompt", GADGET_LIMITS.maxSessionPromptLength);
    return {
      kind: "newSession",
      prompt: prompt.trim(),
      display: requireDisplay(params, GADGET_NAVIGATION_DISPLAYS),
    };
  }
  if (method === "navigation.v1.newAutomation") {
    requireKeys(params, ["name", "prompt"], ["schedule", "display"]);
    const display = requireDisplay(params, ["tab", "page"] as const);
    const name = requireString(params, "name", 200).trim();
    const prompt = requireString(params, "prompt", GADGET_LIMITS.maxSessionPromptLength).trim();
    if (name === "" || prompt === "") {
      throw new BridgeError("INVALID_ARGUMENT", "An automation needs a name and a prompt");
    }
    if (params.schedule === undefined) {
      return { kind: "newAutomation", name, prompt, display };
    }
    const schedule = requireString(params, "schedule", 200).trim();
    if (!RRULE.test(schedule)) {
      throw new BridgeError(
        "INVALID_ARGUMENT",
        "schedule must be an RRULE such as FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
      );
    }
    return { kind: "newAutomation", name, prompt, schedule, display };
  }
  requireKeys(params, ["sessionId"], ["display"]);
  const sessionId = requireString(params, "sessionId", 128);
  if (!SESSION_ID.test(sessionId)) {
    throw new BridgeError("INVALID_ARGUMENT", "Invalid session ID");
  }
  return {
    kind: "openSession",
    sessionId,
    display: requireDisplay(params, GADGET_NAVIGATION_DISPLAYS),
  };
}

/** Converts the SDK's parameters into the RPC route's shape, rejecting anything unexpected. */
export function toRpcParams(
  method: GadgetMethod,
  params: Record<string, unknown>,
): Record<string, unknown> {
  const id = GADGET_LIMITS.maxIdentifierLength;
  const op = GADGET_LIMITS.maxOperationIdLength;
  switch (method) {
    case "data.v1.get":
      requireKeys(params, ["collection", "id"]);
      return {
        collection: requireString(params, "collection", id),
        id: requireString(params, "id", id),
      };
    case "data.v1.list": {
      requireKeys(params, ["collection"], ["limit", "cursor"]);
      const result: { collection: string; limit?: number; cursor?: string } = {
        collection: requireString(params, "collection", id),
      };
      if (params.limit !== undefined) {
        const limit = params.limit;
        if (
          typeof limit !== "number" ||
          !Number.isInteger(limit) ||
          limit < 1 ||
          limit > GADGET_LIMITS.maxListLimit
        ) {
          throw new BridgeError(
            "INVALID_ARGUMENT",
            `"limit" must be an integer from 1 to ${GADGET_LIMITS.maxListLimit}`,
          );
        }
        result.limit = limit;
      }
      if (params.cursor !== undefined) {
        // Cursors are opaque server tokens and can be longer than identifiers.
        result.cursor = requireString(params, "cursor", 4096);
      }
      return result;
    }
    case "data.v1.create": {
      requireKeys(params, ["collection", "value", "operationId"], ["id"]);
      const result: {
        collection: string;
        value: unknown;
        operation_id: string;
        id?: string;
      } = {
        collection: requireString(params, "collection", id),
        value: params.value,
        operation_id: requireString(params, "operationId", op),
      };
      if (params.id !== undefined) {
        result.id = requireString(params, "id", id);
      }
      return result;
    }
    case "data.v1.replace":
      requireKeys(params, ["collection", "id", "value", "ifVersion", "operationId"]);
      return {
        collection: requireString(params, "collection", id),
        id: requireString(params, "id", id),
        value: params.value,
        if_version: requireString(params, "ifVersion", 4096),
        operation_id: requireString(params, "operationId", op),
      };
    case "data.v1.delete":
      requireKeys(params, ["collection", "id", "ifVersion", "operationId"]);
      return {
        collection: requireString(params, "collection", id),
        id: requireString(params, "id", id),
        if_version: requireString(params, "ifVersion", 4096),
        operation_id: requireString(params, "operationId", op),
      };
    case "sessions.v1.list": {
      const result: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(params)) {
        if (!SESSION_PARAM_KEY.test(key)) {
          throw new BridgeError("INVALID_ARGUMENT", "Session parameter names must be alphanumeric");
        }
        result[camelToSnake(key)] = value;
      }
      return result;
    }
    case "navigation.v1.newSession":
    case "navigation.v1.openSession":
    case "navigation.v1.newAutomation":
    case "agent.v1.request":
    case "agent.v1.progress":
    case "ui.v1.selection":
      throw new BridgeError("INTERNAL", "Navigation and action calls are handled by the host");
  }
}

function toSdkDocument(value: unknown): Record<string, unknown> {
  if (!isRecord(value) || typeof value.id !== "string" || typeof value.version !== "string") {
    throw new BridgeError("INTERNAL", "The server returned a malformed document");
  }
  return {
    id: value.id,
    value: value.value,
    version: value.version,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
    createdBy: value.created_by,
    updatedBy: value.updated_by,
  };
}

/** Converts an RPC result into the SDK's camelCase shape. */
export function toSdkResult(method: GadgetMethod, result: unknown): unknown {
  switch (method) {
    case "data.v1.get":
      return result === null || result === undefined ? null : toSdkDocument(result);
    case "data.v1.list": {
      if (!isRecord(result) || !Array.isArray(result.items)) {
        throw new BridgeError("INTERNAL", "The server returned a malformed page");
      }
      const nextCursor = result.next_cursor;
      return {
        items: result.items.map(toSdkDocument),
        nextCursor: typeof nextCursor === "string" && nextCursor !== "" ? nextCursor : null,
      };
    }
    case "data.v1.create":
    case "data.v1.replace":
      return toSdkDocument(result);
    case "data.v1.delete":
      if (
        !isRecord(result) ||
        typeof result.id !== "string" ||
        typeof result.version !== "string"
      ) {
        throw new BridgeError("INTERNAL", "The server returned a malformed deletion");
      }
      return { id: result.id, version: result.version, deleted: true };
    case "sessions.v1.list":
      return isRecord(result) ? snakeToCamelKeys(result) : result;
    case "navigation.v1.newSession":
    case "navigation.v1.openSession":
    case "navigation.v1.newAutomation":
    case "agent.v1.request":
    case "agent.v1.progress":
    case "ui.v1.selection":
      return result;
  }
}

const REQUEST_ID = /^[A-Za-z0-9_.:-]{1,128}$/;

const STATUS_CODES: Record<number, GadgetErrorCode> = {
  400: "INVALID_ARGUMENT",
  401: "UNAUTHENTICATED",
  403: "PERMISSION_DENIED",
  404: "NOT_FOUND",
  409: "CONFLICT",
  413: "INVALID_ARGUMENT",
  429: "RESOURCE_EXHAUSTED",
  501: "UNSUPPORTED",
  502: "UNAVAILABLE",
  503: "UNAVAILABLE",
  504: "UNAVAILABLE",
};

function isRevisionChange(error: GadgetApiError): boolean {
  if (!isRecord(error.details)) {
    return false;
  }
  const reason = error.details.reason;
  return (
    (typeof reason === "string" && reason.toUpperCase() === "REVISION_CHANGED") ||
    typeof error.details.published_revision === "string"
  );
}

/** Maps a failed call to the code and message the frame sees. */
export function toWireError(error: unknown): GadgetWireError {
  if (error instanceof BridgeError) {
    return { code: error.code, message: error.message };
  }
  if (error instanceof GadgetApiError) {
    let code: GadgetWireError["code"];
    if (error.status === 409 && isRevisionChange(error)) {
      code = "REVISION_CHANGED";
    } else if (
      isWellFormedErrorCode(error.code) &&
      error.code !== "REVISION_CHANGED" &&
      error.code !== "DISPOSED"
    ) {
      // Includes codes this web app does not know (for example a future one); the status is
      // only a fallback, because a 409 is not always a version conflict.
      code = error.code;
    } else {
      code = STATUS_CODES[error.status] ?? (error.status >= 500 ? "UNAVAILABLE" : "INTERNAL");
    }
    // The request ID is for support and logs, so it travels beside the message, not in it.
    return {
      code,
      message: error.message.slice(0, MAX_ERROR_MESSAGE_LENGTH),
      ...(error.requestId && REQUEST_ID.test(error.requestId)
        ? { requestId: error.requestId }
        : {}),
    };
  }
  if (error instanceof TypeError) {
    return {
      code: "UNAVAILABLE",
      message: "Omnigent could not reach the server.",
    };
  }
  return { code: "INTERNAL", message: "The request failed." };
}

export function startGadgetBridge(options: StartGadgetBridgeOptions): GadgetBridge {
  const {
    port,
    context,
    callRpc,
    openPage,
    requestAction,
    actionProgress,
    onSelection,
    listSessions,
    onRevisionChanged,
    onClosed,
  } = options;
  const now = options.now ?? Date.now;
  const granted = new Set<GadgetCapability>(context.capabilities);
  const inFlight = new Map<string, AbortController>();
  const recentRequests: number[] = [];
  let violations = 0;
  let closed = false;
  let revisionChanged = false;

  function respond(message: GadgetResponseMessage): void {
    if (closed) {
      return;
    }
    const size = serializedByteLength(message);
    if (message.ok && (size === undefined || size > GADGET_LIMITS.maxMessageBytes)) {
      respond({
        v: GADGET_PROTOCOL_VERSION,
        kind: "response",
        id: message.id,
        ok: false,
        error: {
          code: "RESOURCE_EXHAUSTED",
          message: "The result exceeds the message size budget.",
        },
      });
      return;
    }
    port.postMessage(message);
  }

  function fail(id: string, error: GadgetWireError): void {
    respond({
      v: GADGET_PROTOCOL_VERSION,
      kind: "response",
      id,
      ok: false,
      error,
    });
  }

  function close(reason: GadgetBridgeCloseReason): void {
    if (closed) {
      return;
    }
    for (const [id, controller] of inFlight) {
      controller.abort();
      fail(id, { code: "DISPOSED", message: "The app frame was closed." });
    }
    inFlight.clear();
    closed = true;
    port.removeEventListener("message", handleMessage);
    port.close();
    onClosed?.(reason);
  }

  function recordViolation(): void {
    violations += 1;
    if (violations > GADGET_LIMITS.maxProtocolViolations) {
      close("protocol_violations");
    }
  }

  function withinRateBudget(): boolean {
    const windowStart = now() - 60_000;
    while (recentRequests.length > 0 && recentRequests[0] <= windowStart) {
      recentRequests.shift();
    }
    if (recentRequests.length >= GADGET_LIMITS.maxRequestsPerMinute) {
      return false;
    }
    recentRequests.push(now());
    return true;
  }

  async function run(message: GadgetRequestMessage, controller: AbortController): Promise<void> {
    const timer = setTimeout(() => controller.abort(), message.deadlineMs);
    // Whether this call still owns its ID: cancelling it frees the ID for a new call.
    const stillCurrent = () => inFlight.get(message.id) === controller;
    try {
      if (isGadgetNavigationMethod(message.method)) {
        const request = toPageRequest(message.method, message.params);
        if (!openPage?.(request)) {
          throw new BridgeError(
            "PERMISSION_DENIED",
            "Omnigent opens a page only right after a click in the app.",
          );
        }
        respond({
          v: GADGET_PROTOCOL_VERSION,
          kind: "response",
          id: message.id,
          ok: true,
          result: { opened: true },
        });
        return;
      }
      if (message.method === "agent.v1.request") {
        const actionRequest = toActionRequest(message.params);
        if (!requestAction) {
          throw new BridgeError("UNSUPPORTED", "This host cannot run gadget actions.");
        }
        const outcome = await requestAction(actionRequest, controller.signal);
        if (stillCurrent()) {
          respond({
            v: GADGET_PROTOCOL_VERSION,
            kind: "response",
            id: message.id,
            ok: true,
            result: outcome,
          });
        }
        return;
      }
      if (message.method === "ui.v1.selection") {
        const selection = toSelection(message.params);
        onSelection?.(selection);
        respond({
          v: GADGET_PROTOCOL_VERSION,
          kind: "response",
          id: message.id,
          ok: true,
          result: { shown: Boolean(onSelection) },
        });
        return;
      }
      if (message.method === "agent.v1.progress") {
        const actionIds = toActionProgressRequest(message.params);
        if (!actionProgress) {
          throw new BridgeError("UNSUPPORTED", "This host cannot read gadget action progress.");
        }
        const items = await actionProgress(actionIds, controller.signal);
        if (stillCurrent()) {
          respond({
            v: GADGET_PROTOCOL_VERSION,
            kind: "response",
            id: message.id,
            ok: true,
            result: { items },
          });
        }
        return;
      }
      if (message.method === "sessions.v1.list" && listSessions) {
        const page = await listSessions(toSessionListRequest(message.params), controller.signal);
        if (page === null) {
          throw new BridgeError(
            "PERMISSION_DENIED",
            "The viewer did not allow this gadget to see their sessions.",
          );
        }
        if (stillCurrent()) {
          respond({
            v: GADGET_PROTOCOL_VERSION,
            kind: "response",
            id: message.id,
            ok: true,
            result: page,
          });
        }
        return;
      }
      const rpcParams = toRpcParams(message.method, message.params);
      if (controller.signal.aborted) {
        throw new BridgeError("CANCELLED", "The call was cancelled.");
      }
      const result = await callRpc(message.method, rpcParams, controller.signal);
      if (!stillCurrent()) {
        return;
      }
      respond({
        v: GADGET_PROTOCOL_VERSION,
        kind: "response",
        id: message.id,
        ok: true,
        result: toSdkResult(message.method, result),
      });
    } catch (error) {
      if (!stillCurrent()) {
        return;
      }
      const wireError =
        controller.signal.aborted && !(error instanceof BridgeError)
          ? {
              code: "DEADLINE_EXCEEDED" as const,
              message: "The call did not finish in time.",
            }
          : toWireError(error);
      fail(message.id, wireError);
      if (wireError.code === "REVISION_CHANGED" && !revisionChanged) {
        revisionChanged = true;
        onRevisionChanged?.();
      }
    } finally {
      clearTimeout(timer);
      // A cancelled call's ID can be reused by a new one: leave that one's entry alone.
      if (stillCurrent()) inFlight.delete(message.id);
    }
  }

  function handleMessage(event: MessageEvent): void {
    if (closed) {
      return;
    }
    const parsed = parseFrameMessage(event.data);
    if (!parsed.ok) {
      recordViolation();
      if (parsed.requestId !== undefined && !inFlight.has(parsed.requestId)) {
        fail(parsed.requestId, {
          code: "INVALID_ARGUMENT",
          message: parsed.reason,
        });
      }
      return;
    }
    const message = parsed.message;
    if (message.kind === "cancel") {
      const controller = inFlight.get(message.id);
      if (controller) {
        inFlight.delete(message.id);
        controller.abort();
      }
      return;
    }
    if (inFlight.has(message.id)) {
      recordViolation();
      return;
    }
    if (revisionChanged) {
      fail(message.id, {
        code: "REVISION_CHANGED",
        message: "A newer revision of this app was published.",
      });
      return;
    }
    const required = GADGET_METHOD_CAPABILITY[message.method];
    if (required !== null && !granted.has(required)) {
      fail(message.id, {
        code: "UNSUPPORTED",
        message: `This app revision does not have the ${required} capability.`,
      });
      return;
    }
    if (inFlight.size >= GADGET_LIMITS.maxInFlightRequests || !withinRateBudget()) {
      fail(message.id, {
        code: "RESOURCE_EXHAUSTED",
        message: "Too many calls from this app.",
      });
      return;
    }
    const controller = new AbortController();
    inFlight.set(message.id, controller);
    void run(message, controller);
  }

  port.addEventListener("message", handleMessage);
  port.start?.();
  port.postMessage({ v: GADGET_PROTOCOL_VERSION, kind: "init", context });

  let agentStarting = false;
  return {
    dispose: () => close("disposed"),
    setAgentStarting: (starting) => {
      if (closed || starting === agentStarting) return;
      agentStarting = starting;
      port.postMessage({
        v: GADGET_PROTOCOL_VERSION,
        kind: "event",
        event: "agent.starting",
        starting,
      });
    },
  };
}

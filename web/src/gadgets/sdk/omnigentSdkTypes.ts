// Public TypeScript declarations for `window.omnigent`, the SDK available to uploaded
// gadget HTML. The portable implementation is `omnigentSdkRuntimeSource.ts`; its
// test suite runs that source and checks it against these interfaces. No React or
// app dependencies, so app authors can copy this file into their own project.

import type {
  GadgetCapability,
  GadgetNavigationDisplay,
  GadgetWireErrorCode,
} from "./gadgetProtocol";

export type {
  GadgetCapability,
  GadgetErrorCode,
  GadgetNavigationDisplay,
  GadgetWireErrorCode,
} from "./gadgetProtocol";

export type JsonValue =
  null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

/** One stored record. `version` is opaque; pass it back unchanged as `ifVersion`. */
export interface OmnigentDocument<T extends JsonValue = JsonValue> {
  id: string;
  value: T;
  version: string;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
  updatedBy: string;
}

export interface OmnigentDeletedDocument {
  id: string;
  version: string;
  deleted: true;
}

export interface OmnigentListPage<T extends JsonValue = JsonValue> {
  items: OmnigentDocument<T>[];
  /** Opaque cursor for the next page, or null at the end. Pages are not a consistent snapshot. */
  nextCursor: string | null;
}

export interface OmnigentCallOptions {
  signal?: AbortSignal;
  /** Per-call deadline. Defaults to 15 s; clamped to 0.1–60 s. */
  timeoutMs?: number;
}

export interface OmnigentMutationOptions extends OmnigentCallOptions {
  /**
   * Stable key for one logical change. Reuse it to retry a change whose outcome is
   * unknown (for example after `DEADLINE_EXCEEDED`); the server returns the original
   * result instead of applying the change twice. Defaults to a fresh key per call.
   */
  idempotencyKey?: string;
}

export interface OmnigentCreateOptions extends OmnigentMutationOptions {
  /** Record ID to create; the server assigns one when omitted. */
  id?: string;
}

export interface OmnigentConditionalOptions extends OmnigentMutationOptions {
  /** Version the change is based on. The call fails with `CONFLICT` if the record moved on. */
  ifVersion: string;
}

export type OmnigentWatchStatus = "loading" | "live" | "reconnecting" | "stale";

export interface OmnigentWatchSnapshot<T extends JsonValue = JsonValue> {
  status: OmnigentWatchStatus;
  /** Last successfully read first page; kept while reconnecting or stale. */
  items: OmnigentDocument<T>[];
  nextCursor: string | null;
  /** Time of the last successful read (ISO 8601), or null before the first one. */
  updatedAt: string | null;
  error: OmnigentError | null;
}

export interface OmnigentWatchOptions {
  /** Poll interval; at least 1000 ms. Defaults to 2000 ms. */
  intervalMs?: number;
  /** Page size of the watched first page. Defaults to 100, the maximum. */
  limit?: number;
}

export interface OmnigentDisposable {
  dispose: () => void;
}

export interface OmnigentCollection<T extends JsonValue = JsonValue> {
  readonly name: string;
  get: (id: string, options?: OmnigentCallOptions) => Promise<OmnigentDocument<T> | null>;
  list: (
    page?: { limit?: number; cursor?: string },
    options?: OmnigentCallOptions,
  ) => Promise<OmnigentListPage<T>>;
  create: (value: T, options?: OmnigentCreateOptions) => Promise<OmnigentDocument<T>>;
  /** Replaces the whole value; this is not a merge. */
  replace: (
    id: string,
    value: T,
    options: OmnigentConditionalOptions,
  ) => Promise<OmnigentDocument<T>>;
  delete: (id: string, options: OmnigentConditionalOptions) => Promise<OmnigentDeletedDocument>;
  /**
   * Polls the first page every `intervalMs` and calls `listener` with each snapshot, starting with
   * `loading`: `collection.watch((snapshot) => render(snapshot.items), { intervalMs: 2000 })`.
   * It is not an async iterator.
   */
  watch: (
    listener: (snapshot: OmnigentWatchSnapshot<T>) => void,
    options?: OmnigentWatchOptions,
  ) => OmnigentDisposable;
}

export interface OmnigentClientContext {
  app: { id: string; name: string };
  revisionId: string;
  /**
   * `id` is opaque and stable within this gadget, and is what records' `createdBy` and
   * `updatedBy` hold for the viewer, so `record.createdBy === context.viewer.id` tells the
   * viewer's own. `displayName` is a real name, or `null` when the server has none; never a
   * login or email.
   */
  viewer: { id: string; displayName: string | null };
  permissions: { writeData: boolean; publish: boolean };
  /**
   * True in a template preview, where data lives in memory, nothing is saved and actions are off.
   * Show sample content and skip sign-in, saving and access notices there.
   */
  preview: boolean;
}

export interface OmnigentSessions {
  /** False until the host grants `sessions.read` for this revision; calls then fail with `UNSUPPORTED`. */
  readonly canList: boolean;
  list: (params?: Record<string, JsonValue>, options?: OmnigentCallOptions) => Promise<JsonValue>;
}

/** Opens Omnigent pages in a new tab. Each call must follow a click inside the app. */
/**
 * Where a page opens: `tab` (default) a new browser tab; `panel` a pane beside this app, which
 * keeps running; `page` this tab in place of the app, with Back returning to it.
 */
export type OmnigentNavigationDisplay = GadgetNavigationDisplay;

export interface OmnigentNavigation {
  /**
   * Opens New session with `prompt` filled in. The viewer chooses where it runs and starts it;
   * Omnigent appends how the session can reach this app's shared records. With `panel`, the
   * pane shows the session once the viewer starts it.
   */
  newSession: (
    params?: { prompt?: string; display?: OmnigentNavigationDisplay },
    options?: OmnigentCallOptions,
  ) => Promise<{ opened: true }>;
  /**
   * Opens a session. A viewer without access to it sees Omnigent's usual message. In a `panel`
   * the gadget shows beside that session; its actions still go to the viewer's gadget session.
   */
  openSession: (
    sessionId: string,
    options?: OmnigentCallOptions & { display?: OmnigentNavigationDisplay },
  ) => Promise<{ opened: true }>;
  /**
   * Opens Automations with the create dialog filled in. `schedule` is an RRULE, for example
   * `FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=9;BYMINUTE=0`.
   * The viewer picks where it runs and saves it; Omnigent appends the same note as for sessions.
   * Automations open in a `tab` (default) or the `page`, not a panel.
   */
  newAutomation: (
    params: { name: string; prompt: string; schedule?: string; display?: "tab" | "page" },
    options?: OmnigentCallOptions,
  ) => Promise<{ opened: true }>;
}

/**
 * What `agent.request` resolves with. Follow the action with `receipt.watch(listener)`, the same
 * as `client.agent.watch(listener, { actionIds: [receipt.actionId] })`.
 */
export interface OmnigentActionReceipt {
  /** The action's entry in the gadget's ledger. */
  actionId: string;
  /** The same as `actionId`. */
  id: string;
  /**
   * `sent` to the viewer's gadget session, `starting` while they start one beside the gadget, or
   * `merged` when the same action with the same params was still open: `actionId` is that one.
   */
  delivery: "sent" | "starting" | "merged";
  /** Follows this action; see `OmnigentAgent.watch`. */
  watch: (
    listener: (snapshot: OmnigentActionWatchSnapshot) => void,
    options?: { intervalMs?: number },
  ) => OmnigentActionWatch;
}

/**
 * How far an action has got. Omnigent derives `status` from the sessions doing the work (their
 * live status, runner liveness, pending approvals and errors), not from what the agent reports,
 * so an action never stays running after its sessions stop:
 * - `pending`: requested, not delivered to the gadget session yet (the viewer has not started it).
 * - `queued`: delivered; the gadget session is busy with an earlier action or has not started.
 * - `running`, or `needs_input` when a session doing the work waits for the viewer.
 * - `done`, `failed`, `cancelled`: how the agent reported it ended.
 * - `finished_unreported`: its sessions stopped without the agent reporting how it went.
 * - `interrupted`: a session failed, went offline or never started it; `reason` says which.
 * - `not_found`. Handle unknown statuses as finished.
 */
export interface OmnigentActionProgress {
  id: string;
  status: string;
  reason: string | null;
  action: string | null;
  params: { [key: string]: JsonValue } | null;
  /** The agent's Markdown summary, if it wrote one. */
  summary: string | null;
  /** The session the agent recorded as doing the work. */
  sessionId: string | null;
  /** The sessions the status was derived from. */
  sessionIds: string[];
  /** When it was requested (ISO 8601), or null when the host does not say. */
  requestedAt: string | null;
}

/**
 * One report of `agent.watch`. `status` is the watch's own state, not an action's: read each
 * action's status from `items` (or `action`).
 */
export interface OmnigentActionWatchSnapshot {
  /** `loading` before the first read, then `live`; `reconnecting` or `stale` after failed reads. */
  status: OmnigentWatchStatus;
  /** One entry per watched action (or the viewer's recent actions when none were given). */
  items: OmnigentActionProgress[];
  /** When exactly one action is watched: its entry, or null until it is read. Otherwise null. */
  action: OmnigentActionProgress | null;
  /** Time of the last successful read (ISO 8601), or null before the first one. */
  updatedAt: string | null;
  error: OmnigentError | null;
}

export interface OmnigentActionWatch extends OmnigentDisposable {
  /** Reads progress again now, for example right after `agent.request`. */
  refresh: () => void;
}

export interface OmnigentAgent {
  /**
   * Asks the viewer's gadget session (one per viewer and gadget, shown beside it) to carry out
   * one of the gadget's declared actions. Needs a click inside the app and edit access to the
   * gadget (`PERMISSION_DENIED` otherwise). The session records progress in the gadget's
   * action ledger and, as the action's description says, in the gadget's records.
   * `idempotencyKey` names one logical request: after an unknown outcome (a timeout), asking
   * again with it, or with the error's `operationId`, returns the first receipt, even once that
   * action finished, instead of running it again.
   */
  request: (
    action: string,
    params?: { [key: string]: JsonValue },
    options?: OmnigentMutationOptions,
  ) => Promise<OmnigentActionReceipt>;
  /** Progress of up to 20 actions, or of the viewer's recent actions (at most 10) when none are given. */
  progress: (
    actionIds?: string[],
    options?: OmnigentCallOptions,
  ) => Promise<{ items: OmnigentActionProgress[] }>;
  /**
   * Polls progress every `intervalMs` (default 3000) and calls `listener` with each snapshot,
   * starting with `loading`. It is not an async iterator. Without `actionIds` it follows the
   * viewer's recent actions, so a reloaded gadget shows them again. Example:
   *
   *     const receipt = await client.agent.request("refresh", {});
   *     const watch = client.agent.watch((snapshot) => {
   *       const progress = snapshot.action; // or snapshot.items[0]
   *       if (progress && !["pending", "queued", "running", "needs_input"].includes(progress.status)) {
   *         watch.dispose(); // done, failed, cancelled, finished_unreported or interrupted
   *       }
   *     }, { actionIds: [receipt.actionId] });
   *
   * One action can also be named directly: `watch(actionId, listener)`,
   * `watch(listener, actionId)`, `watch(receipt, listener)` or `watch(listener, { actionId })`.
   */
  watch: OmnigentWatch;
  /**
   * Whether the viewer's gadget session is being created now: from their Create session until
   * that session has the requests made before it. Meanwhile requests are refused with
   * `UNAVAILABLE`, and the SDK holds clicks on controls marked `data-omnigent-requires="action"`
   * and marks the page root `data-omnigent-session-starting`. Show those controls as waiting,
   * for example "Starting session…".
   */
  starting: () => boolean;
  /** Calls `listener` with `starting()` each time it changes. Returns an unsubscribe. */
  onStarting: (listener: (starting: boolean) => void) => () => void;
}

type OmnigentWatchListener = (snapshot: OmnigentActionWatchSnapshot) => void;

/** The forms `agent.watch` takes. */
export interface OmnigentWatch {
  (
    listener: OmnigentWatchListener,
    options?: { actionIds?: string[]; actionId?: string; intervalMs?: number },
  ): OmnigentActionWatch;
  (actionId: string, listener: OmnigentWatchListener): OmnigentActionWatch;
  (listener: OmnigentWatchListener, actionId: string): OmnigentActionWatch;
  (receipt: OmnigentActionReceipt, listener: OmnigentWatchListener): OmnigentActionWatch;
}

/**
 * What a control needs: `write` changes records (edit access to the gadget's data and the
 * `data.write` capability), `action` runs a declared action (Can edit access to the gadget).
 * Mark such controls with `data-omnigent-requires="write"` or `"action"` (space-separated for
 * both): for a viewer without that access the SDK disables them (read-only for text fields, so
 * their text stays selectable), sets `aria-disabled` and `data-omnigent-blocked`, shows the reason
 * as their tooltip, and keeps them that way even if the page re-enables them.
 */
export type OmnigentRequirement = "write" | "action";

export interface OmnigentClient {
  readonly context: OmnigentClientContext;
  readonly capabilities: readonly GadgetCapability[];
  hasCapability: (capability: GadgetCapability) => boolean;
  /** Whether this viewer may do what `requirement` needs. */
  can: (requirement: OmnigentRequirement) => boolean;
  data: {
    collection: <T extends JsonValue = JsonValue>(name: string) => OmnigentCollection<T>;
  };
  sessions: OmnigentSessions;
  navigation: OmnigentNavigation;
  agent: OmnigentAgent;
  ui: {
    readonly colorScheme: "light" | "dark";
    /** Why this viewer can't do what `requirement` needs, for the page's own messages; null when they can. */
    blockedReason: (requirement: OmnigentRequirement) => string | null;
  };
  /** Rejects outstanding calls with `DISPOSED`, stops watches, and closes the port. */
  dispose: () => void;
}

export interface OmnigentError extends Error {
  readonly name: "OmnigentError";
  /** One of `GadgetErrorCode`, or a newer server code; handle unknown codes as a generic failure. */
  readonly code: GadgetWireErrorCode;
  /** True only for failures where repeating the same call is safe. Mutations are never marked retryable. */
  readonly retryable: boolean;
  /** For mutations: the idempotency key to pass back when retrying an unknown outcome. */
  readonly operationId?: string;
  /** The server's request ID, for logs and support; keep it out of messages shown to people. */
  readonly requestId?: string;
}

export interface OmnigentSdk {
  readonly version: 1;
  /** Resolves once the Omnigent host has sent this frame's context. Repeated calls share one client. */
  connect: (options?: { timeoutMs?: number }) => Promise<OmnigentClient>;
  isOmnigentError: (error: unknown) => error is OmnigentError;
}

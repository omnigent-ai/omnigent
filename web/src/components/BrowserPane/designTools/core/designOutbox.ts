// The outbox for Browser-tab design requests: a tool hands a request off here, and the
// outbox sends it through the chat store (as `external`) once its session is idle, one at
// a time, keeping it (saved in designOutboxStorage) to retry until it's sent.
import { create } from "zustand";
import { browserViewOwnerId } from "@/hooks/useBrowserTabs";
import { shouldQueueSend } from "@/lib/messageQueue";
import {
  fetchSignedInUserId,
  getCurrentUserId,
  resolveIdentity,
  subscribeIdentity,
} from "@/lib/identity";
import { randomUUID } from "@/lib/randomUUID";
import {
  holdsInitialDraft,
  onSendDelivered,
  useChatStore,
  wouldHoldSend,
  type ConversationState,
} from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import { nativePendingId } from "./pendingInputId";
import { setDesignRequestSessions } from "./designOutboxPresence";
import {
  adoptSavedRequests,
  deleteRequest,
  saveRequest,
  savedFilesOf,
  type SavedRequest,
} from "./designOutboxStorage";

export type DesignRequestStatus = "waiting" | "sending" | "sent" | "failed" | "denied";

export interface DesignRequest {
  /** The message's stable id: every attempt posts under it. */
  id: string;
  tabId: string;
  conversationId: string;
  /** What the tool sent, for the outbox's list ("Annotate: button …"). */
  label: string;
  text: string;
  files: readonly File[];
  status: DesignRequestStatus;
  /** Sending, and the server hasn't answered for a while. */
  slow?: boolean;
  /** Why it isn't sent, for the user. */
  error?: string;
  /**
   * Not sent: its last attempt was refused (it never reached the agent), or may have reached
   * it unseen (unanswered, or from before a reload), so the user looks in the chat first.
   */
  delivery?: "refused" | "unknown";
}

interface OutboxState {
  requests: DesignRequest[];
}

export const useDesignOutbox = create<OutboxState>(() => ({ requests: [] }));

/** How long a send runs before the outbox says the server is slow (it keeps waiting). */
export const SLOW_SEND_MS = 60_000;
/**
 * How long a send runs before the outbox stops waiting on it, so the session's next requests
 * aren't held behind a POST that never answers. It may still have arrived: its Retry asks first.
 */
export const STALLED_SEND_MS = 180_000;
/** A sent request should start a turn; past this, the next one goes anyway. */
export const TURN_START_MS = 15_000;
/** How long a sent request stays listed (for its notice) before it's dropped. */
const SENT_KEEP_MS = 10_000;
/** After restoring failed, the wait before trying again (doubling, up to the cap). */
export const RESTORE_RETRY_MS = 5_000;
const RESTORE_RETRY_MAX_MS = 300_000;

const DENIED = "A policy blocked this request, so it wasn't sent.";
// Brought back after a reload: never sent on its own (the user said Send on an earlier
// page, and may have changed their mind, or sent it from another tab), so it needs a Retry.
const RESTORED_WAITING = "Brought back after the app reloaded. Retry to send it.";
const RESTORED_SENDING =
  "The app reloaded while this was sending, so it may have reached the agent. " +
  "Look in the chat: if it isn't there, Retry sends it.";
// The server's refusal when the session's host is offline promises a resend the chat box
// makes for its own messages; the outbox doesn't, so it says what to do instead.
const HOST_OFFLINE = /host for this session is offline/i;
const HOST_OFFLINE_ERROR =
  "The session's host is offline, so this wasn't sent. Retry once it's back.";
const LOOK = "Look in the chat: if it isn't there, Retry sends it.";
const STALLED = `The server didn't answer, so this may have reached the agent. ${LOOK}`;

// Each request's current attempt: a send the outbox stopped waiting on (or that a
// later one replaced) is heard only if it turns out to have arrived.
const attempts = new Map<string, number>();
// Each send still running in the chat store, by request: removing the request stops it
// before it posts (its uploads can outlast the outbox's wait).
const running = new Map<string, AbortController>();
// The pending-input id a native terminal session gives each request, by request.
const pendingIds = new Map<string, string>();
// Requests heard to have arrived while their send still ran: its failure then doesn't count.
const arrived = new Set<string>();

/** Learn the pending-input id `request` would have, for hearing that it arrived. */
function notePendingId(request: Pick<DesignRequest, "id" | "conversationId">): void {
  void nativePendingId(request.conversationId, request.id).then(
    (pendingId) => pendingIds.set(pendingId, request.id),
    () => {},
  );
}

// Per session, after a send: wait for its turn to start and end before the next one,
// so a second request doesn't land in (and steer) the first one's turn.
const turns = new Map<string, { postedAt: number; sawBusy: boolean }>();

function update(id: string, patch: Partial<DesignRequest>): void {
  useDesignOutbox.setState((state) => ({
    requests: state.requests.map((request) =>
      request.id === id ? { ...request, ...patch } : request,
    ),
  }));
}

/** A session's state, if this tab has it: on screen, or still live in the registry. */
function sessionState(conversationId: string): ConversationState | null {
  const root = useChatStore.getState();
  if (root.conversationId === conversationId) return root;
  return conversationRegistry.peek(conversationId)?.getState() ?? null;
}

const isBusy = (state: ConversationState) =>
  state.status === "streaming" || state.sessionStatus === "running";

/** The agent to send to, when `conversationId` can take a design request now. */
function readyAgent(conversationId: string): string | null {
  const state = sessionState(conversationId);
  // Not loaded here: wait until it is, rather than guess it's idle.
  if (!state?.boundAgentId) return null;
  const turn = turns.get(conversationId);
  if (turn) {
    if (isBusy(state)) turn.sawBusy = true;
    const settled = turn.sawBusy ? !isBusy(state) : Date.now() - turn.postedAt > TURN_START_MS;
    if (!settled) return null;
    turns.delete(conversationId);
  }
  // The composer's rule for "queue, don't send" (busy, its queued messages first, a first
  // message held), plus the store's holds: `send` must never hold an outbox request.
  const queue = shouldQueueSend(
    conversationId,
    state.status,
    state.sessionStatus,
    useChatStore.getState().queuedMessages,
    false,
    false,
    holdsInitialDraft(state),
  );
  if (queue || state.startupPending || wouldHoldSend(state)) return null;
  return state.boundAgentId;
}

/**
 * Send `request` through the chat store. Called in the same tick as `readyAgent`, so
 * the store never holds it: it resolves having posted, or reports why not.
 */
async function send(request: DesignRequest, agentId: string): Promise<void> {
  // An earlier attempt the outbox stopped waiting on (a stalled upload) mustn't post too.
  running.get(request.id)?.abort();
  const attempt = (attempts.get(request.id) ?? 0) + 1;
  attempts.set(request.id, attempt);
  const current = () => attempts.get(request.id) === attempt;
  arrived.delete(request.id);
  update(request.id, { status: "sending", slow: false, error: undefined, delivery: undefined });
  const controller = new AbortController();
  running.set(request.id, controller);
  const slow = setTimeout(() => update(request.id, { slow: true }), SLOW_SEND_MS);
  const stalled = setTimeout(() => {
    if (!current()) return;
    attempts.set(request.id, attempt + 1);
    if (arrived.has(request.id)) {
      // Its turn is running: let the send run on rather than fail that turn.
      if (running.get(request.id) === controller) running.delete(request.id);
      markSent(request);
    } else {
      update(request.id, { status: "failed", slow: false, error: STALLED, delivery: "unknown" });
      // Stopping it settles the chat store's send, so the session takes its next message.
      controller.abort();
    }
    schedule();
  }, STALLED_SEND_MS);
  let failure: string | null = null;
  let refused = false;
  let denied = false;
  try {
    await useChatStore.getState().send(request.text, agentId, [...request.files], {
      stableId: request.id,
      pinnedConversationId: request.conversationId,
      external: true,
      signal: controller.signal,
      retriesRefused: request.delivery === "refused",
      onError: (message, wasRefused) => {
        failure = message;
        refused = wasRefused === true;
      },
      onDenied: () => {
        denied = true;
      },
      // Stopped while its POST was out, which then went through: it arrived.
      onAcceptedLate: () => delivered(request.id),
    });
  } catch (error) {
    failure = error instanceof Error ? error.message : String(error);
  } finally {
    clearTimeout(slow);
    clearTimeout(stalled);
    if (running.get(request.id) === controller) running.delete(request.id);
  }
  if (!current()) {
    // Given up on, or replaced: only news that it arrived still counts.
    if (!denied && failure === null) delivered(request.id);
    return;
  }
  if (denied) {
    update(request.id, { status: "denied", slow: false, error: DENIED });
  } else if (failure !== null && !arrived.has(request.id)) {
    // Refused: it never reached the agent. Otherwise it may have (an unanswered POST).
    const error = !refused
      ? `Couldn't tell whether this reached the agent (${failure}). ${LOOK}`
      : HOST_OFFLINE.test(failure)
        ? HOST_OFFLINE_ERROR
        : `Couldn't send: ${failure}`;
    const delivery = refused ? "refused" : "unknown";
    update(request.id, { status: "failed", slow: false, error, delivery });
  } else {
    markSent(request);
  }
  schedule();
}

function markSent(request: DesignRequest): void {
  arrived.delete(request.id);
  turns.set(request.conversationId, { postedAt: Date.now(), sawBusy: false });
  update(request.id, { status: "sent", slow: false, error: undefined, delivery: undefined });
  // If no turn starts, nothing in the stores changes: look again once the wait is over.
  setTimeout(schedule, TURN_START_MS + 1);
  setTimeout(() => forget(request.id), SENT_KEEP_MS);
}

/**
 * `id` reached the agent: one the outbox holds as not sent (its POST failed or went
 * unanswered first, or it's waiting for a Retry) is sent, and won't go again.
 */
function delivered(id: string): void {
  const requestId = pendingIds.get(id) ?? id;
  const request = useDesignOutbox.getState().requests.find((item) => item.id === requestId);
  // Sending: its send hears of it when it ends, whatever the POST's answer was.
  if (request?.status === "sending") arrived.add(request.id);
  if (request?.status !== "failed" && request?.status !== "waiting") return;
  attempts.set(request.id, (attempts.get(request.id) ?? 0) + 1);
  markSent(request);
  schedule();
}
onSendDelivered(delivered);

/** Post the next waiting request of each session that's ready for one. */
export function processDesignOutbox(): void {
  const { requests } = useDesignOutbox.getState();
  const sessions = new Set(requests.map((request) => request.conversationId));
  for (const conversationId of sessions) {
    const own = requests.filter((request) => request.conversationId === conversationId);
    if (own.some((request) => request.status === "sending")) continue;
    const next = own.find((request) => request.status === "waiting");
    if (!next) continue;
    const agentId = readyAgent(conversationId);
    if (!agentId) continue;
    void send(next, agentId);
  }
}

function schedule(): void {
  queueMicrotask(processDesignOutbox);
}

export interface NewDesignRequest {
  tabId: string;
  label: string;
  text: string;
  files?: readonly File[];
}

/** Hand a request to the outbox; returns its id. */
export function queueDesignRequest({ tabId, label, text, files = [] }: NewDesignRequest): string {
  const id = randomUUID().replace(/-/g, "");
  const request: DesignRequest = {
    id,
    tabId,
    conversationId: browserViewOwnerId(tabId),
    label,
    text,
    files,
    status: "waiting",
  };
  notePendingId(request);
  useDesignOutbox.setState((state) => ({ requests: [...state.requests, request] }));
  schedule();
  return id;
}

/**
 * Send a failed request again, under its id: the user's call, as one that may have
 * arrived says to look in the chat first.
 */
export function retryDesignRequest(id: string): void {
  const request = useDesignOutbox.getState().requests.find((item) => item.id === id);
  if (request?.status !== "failed") return;
  update(id, { status: "waiting", error: undefined });
  schedule();
}

/** Drop a request that isn't sending, stopping a send of it the outbox stopped waiting on. */
export function forget(id: string): void {
  const held = useDesignOutbox.getState().requests.find((item) => item.id === id);
  if (held && held.status !== "sending") {
    running.get(id)?.abort();
    arrived.delete(id);
  }
  useDesignOutbox.setState((state) => ({
    requests: state.requests.filter((request) => request.id !== id || request.status === "sending"),
  }));
}

/** Requests that need the user: not yet sent, or not sent at all. */
export const unsettled = (request: DesignRequest) => request.status !== "sent";

// Wake waiting requests when a session changes (a turn ends, a hold lifts): the one on
// screen lives on the chat store, the others in the registry.
const wake = () => {
  if (useDesignOutbox.getState().requests.some((request) => request.status === "waiting")) {
    schedule();
  }
};
useChatStore.subscribe(wake);
conversationRegistry.subscribe(wake);

// Tell the chat which sessions have requests to show.
useDesignOutbox.subscribe(({ requests }) =>
  setDesignRequestSessions(
    requests.filter((request) => request.status !== "sent").map((r) => r.conversationId),
  ),
);

// Keep what isn't sent saved, in the order it changed. A request's text and files never
// change, so only its status (and why) is worth a new save. A request counts as saved only
// once its write succeeded; one whose save failed is tried again on the next change.
const savedAs = new Map<string, string>();
const saving = new Map<string, string>();
// Requests removed here whose saved copy isn't deleted yet: a failed delete is tried
// again, so a removed request can't come back (and send itself) on the next page.
const deleting = new Map<string, "pending" | "failed">();

function forgetSaved(id: string): void {
  deleting.set(id, "pending");
  void write(() => deleteRequest(id)).then((ok) => {
    if (ok) deleting.delete(id);
    else deleting.set(id, "failed");
  });
}
let writes: Promise<void> = Promise.resolve();

const savedForm = (request: DesignRequest) =>
  request.status === "sent"
    ? "sent"
    : `${request.status}:${request.error ?? ""}:${request.delivery ?? ""}`;

function write(task: () => Promise<void>): Promise<boolean> {
  const result = writes.then(task).then(
    () => true,
    () => false,
  );
  writes = result.then(() => undefined);
  return result;
}

function persist(requests: readonly DesignRequest[]): void {
  const present = new Set<string>();
  for (const request of requests) {
    present.add(request.id);
    const form = savedForm(request);
    if (savedAs.get(request.id) === form || saving.get(request.id) === form) continue;
    saving.set(request.id, form);
    let unknownUser = false;
    void write(() => {
      if (request.status === "sent") return deleteRequest(request.id);
      // Who it belongs to as the write runs: the user may have become known meanwhile.
      const userId = getCurrentUserId();
      unknownUser = userId === null;
      if (userId === null) return Promise.reject(new Error("Not signed in yet"));
      return saveRequest(request, userId);
    }).then((ok) => {
      if (saving.get(request.id) === form) saving.delete(request.id);
      if (ok) savedAs.set(request.id, form);
      else if (unknownUser) saveOnceUserKnown();
    });
  }
  for (const id of new Set([...savedAs.keys(), ...saving.keys()])) {
    if (present.has(id)) continue;
    const wasSent = savedAs.get(id) === "sent";
    savedAs.delete(id);
    saving.delete(id);
    if (!wasSent) forgetSaved(id);
  }
  for (const [id, state] of deleting) {
    if (state === "failed" && !present.has(id)) forgetSaved(id);
  }
}
useDesignOutbox.subscribe(({ requests }) => persist(requests));

// Requests queued before the user was known couldn't be saved: save them once they are.
let awaitingUser: (() => void) | null = null;
function saveOnceUserKnown(): void {
  if (getCurrentUserId() !== null) {
    persist(useDesignOutbox.getState().requests);
    return;
  }
  awaitingUser ??= subscribeIdentity(() => {
    if (getCurrentUserId() === null) return;
    awaitingUser?.();
    awaitingUser = null;
    persist(useDesignOutbox.getState().requests);
  });
}

/**
 * Whether storage is behind: a request that isn't sent lacks a confirmed save (leaving
 * would lose it), or a sent or removed one's saved copy isn't deleted yet (it would come back).
 */
function unsaved(): boolean {
  if (deleting.size > 0) return true;
  return useDesignOutbox
    .getState()
    .requests.some((request) => savedAs.get(request.id) !== savedForm(request));
}

// The timer to try restoring again.
let restoreTimer: ReturnType<typeof setTimeout> | null = null;
let restoreRetryMs = RESTORE_RETRY_MS;

function later(ms: number, task: () => void): void {
  if (restoreTimer !== null) clearTimeout(restoreTimer);
  restoreTimer = setTimeout(() => {
    restoreTimer = null;
    task();
  }, ms);
}

/**
 * Bring back the requests this user saved in this tab before a reload (and in tabs now
 * closed). None sends on its own: each needs a Retry, and one that was sending says it
 * may have arrived.
 */
export async function restoreSavedDesignRequests(): Promise<void> {
  const userId = getCurrentUserId() ?? (await resolveIdentity());
  if (userId === null) {
    // Not signed in yet: only that user's requests may come back, so wait to know who.
    const stop = subscribeIdentity(() => {
      if (getCurrentUserId() === null) return;
      stop();
      void restoreSavedDesignRequests();
    });
    return;
  }
  // The cached user can be stale: adopt only for the account the server says is signed in.
  const signedIn = await fetchSignedInUserId();
  if (signedIn !== userId) {
    if (signedIn === undefined) later(restoreRetryMs, () => void restoreSavedDesignRequests());
    return;
  }
  let saved: SavedRequest[];
  try {
    const held = new Set(useDesignOutbox.getState().requests.map((request) => request.id));
    saved = await adoptSavedRequests(userId, held);
  } catch {
    // Storage may recover: try again, less often each time (none without IndexedDB at all).
    if (globalThis.indexedDB) later(restoreRetryMs, () => void restoreSavedDesignRequests());
    restoreRetryMs = Math.min(restoreRetryMs * 2, RESTORE_RETRY_MAX_MS);
    return;
  }
  restoreRetryMs = RESTORE_RETRY_MS;
  const known = new Set(useDesignOutbox.getState().requests.map((request) => request.id));
  const restored: DesignRequest[] = saved
    .filter((record) => !known.has(record.id))
    .map((record) => ({
      id: record.id,
      tabId: record.tabId,
      conversationId: record.conversationId,
      label: record.label,
      text: record.text,
      files: savedFilesOf(record),
      // Another page may have sent it since: it says to look in the chat first.
      delivery: record.refused ? ("refused" as const) : ("unknown" as const),
      ...(record.status === "waiting"
        ? { status: "failed" as const, error: RESTORED_WAITING }
        : record.status === "sending"
          ? { status: "failed" as const, error: RESTORED_SENDING }
          : { status: record.status, error: record.error }),
    }));
  if (restored.length === 0) return;
  for (const request of restored) notePendingId(request);
  useDesignOutbox.setState((state) => ({ requests: [...restored, ...state.requests] }));
}

if (typeof window !== "undefined") {
  void restoreSavedDesignRequests();
  // Leaving asks first only while a request that isn't sent has no confirmed save.
  window.addEventListener("beforeunload", (event) => {
    if (unsaved()) event.preventDefault();
  });
}

/** Test hook: resolves once every save so far has finished. */
export function designOutboxSaved(): Promise<void> {
  return writes;
}

/** Test hook: an empty outbox and no per-session waits. */
export function resetDesignOutbox(): void {
  useDesignOutbox.setState({ requests: [] });
  turns.clear();
  attempts.clear();
  running.clear();
  pendingIds.clear();
  arrived.clear();
  if (restoreTimer !== null) clearTimeout(restoreTimer);
  restoreTimer = null;
  restoreRetryMs = RESTORE_RETRY_MS;
  awaitingUser?.();
  awaitingUser = null;
  savedAs.clear();
  saving.clear();
  deleting.clear();
}

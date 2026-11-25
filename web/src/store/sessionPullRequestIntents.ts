// Links and unlinks the user asked for that the server's list doesn't show yet. They overlay
// the list wherever a session's PRs show, so a link shows at once; only the list's own reads
// ever write it, so a late reply can't undo a newer change.

import { create } from "zustand";

import { pullRequestKey } from "@/lib/pullRequestReference";

export type PullRequestIntentAction = "link" | "unlink";

export interface PullRequestIntent {
  id: number;
  url: string;
  action: PullRequestIntentAction;
  /** Its request answered, or can't say how it ended: only the server's list decides now. */
  requestDone: boolean;
  /** The list was read once more after it disagreed or couldn't be read. */
  retried: boolean;
  /** What to say if the server's list, read again, still disagrees. */
  unconfirmedReason: string;
}

/** A session's intents, by PR key. */
export type SessionPullRequestIntents = Readonly<Record<string, PullRequestIntent>>;

interface IntentsState {
  bySession: Readonly<Record<string, SessionPullRequestIntents>>;
  /** Links the server's list never showed, by session and PR key, until it's linked again. */
  unconfirmed: Readonly<Record<string, Readonly<Record<string, string>>>>;
}

/**
 * How an intent left the store: the list showed it, its request was refused, a newer intent for
 * the same PR replaced it, or the list never showed it in time (with what to say).
 */
export type PullRequestIntentEnd =
  | { status: "settled" }
  | { status: "refused" }
  | { status: "replaced" }
  | { status: "unconfirmed"; reason: string };

export const LINK_UNCONFIRMED_REASON =
  "The server didn’t keep the link. Check this session’s pull requests, then try again.";
export const LINK_UNREADABLE_REASON =
  "Couldn’t confirm the link. Check this session’s pull requests, then try again.";
export const UNLINK_UNCONFIRMED_REASON =
  "The server still links this pull request. Try removing it again.";
export const UNLINK_UNREADABLE_REASON =
  "Couldn’t confirm the removal. Check this session’s pull requests, then try again.";

/** How long an intent may wait on its request and the reads after it before it gives up. */
export const PULL_REQUEST_INTENT_LIFETIME_MS = 60_000;

/** A newer link or unlink of the same PR took over; this one reports nothing. */
export class PullRequestIntentReplaced extends Error {
  constructor() {
    super("A newer change to this pull request decides how it shows.");
    this.name = "PullRequestIntentReplaced";
  }
}

/**
 * Whether a failed link or unlink may still have gone through: the answer was lost, the server
 * failed, or it refused after the host had already recorded it. A refusal the request itself
 * caused, such as a PR the host can't read, is final.
 */
export function pullRequestChangeIsUnclear(error: unknown): boolean {
  const status = (error as { status?: unknown } | null)?.status;
  if (typeof status !== "number") return true;
  return status === 409 || status === 408 || status >= 500;
}

export const useSessionPullRequestIntentStore = create<IntentsState>(() => ({
  bySession: {},
  unconfirmed: {},
}));

let nextIntentId = 1;
/** Who waits on each intent's end, and the timer that bounds its life; both go when it ends. */
const waiters = new Map<number, (end: PullRequestIntentEnd) => void>();
const deadlines = new Map<number, ReturnType<typeof setTimeout>>();

function withEntry<Value>(
  record: Readonly<Record<string, Readonly<Record<string, Value>>>>,
  sessionId: string,
  key: string,
  value: Value | undefined,
): Readonly<Record<string, Readonly<Record<string, Value>>>> {
  const { [key]: _old, ...rest } = record[sessionId] ?? {};
  const next = value === undefined ? rest : { ...rest, [key]: value };
  const { [sessionId]: _session, ...others } = record;
  return Object.keys(next).length > 0 ? { ...others, [sessionId]: next } : others;
}

function current(sessionId: string, id: number): PullRequestIntent | undefined {
  const intents = useSessionPullRequestIntentStore.getState().bySession[sessionId] ?? {};
  return Object.values(intents).find((intent) => intent.id === id);
}

function ended(id: number, end: PullRequestIntentEnd): void {
  clearTimeout(deadlines.get(id));
  deadlines.delete(id);
  const waiter = waiters.get(id);
  waiters.delete(id);
  waiter?.(end);
}

const unreadableReason = (intent: PullRequestIntent) =>
  intent.action === "link" ? LINK_UNREADABLE_REASON : UNLINK_UNREADABLE_REASON;

/** Take `intent` out of the store; a link that never showed leaves its reason behind. */
function drop(sessionId: string, intent: PullRequestIntent, end: PullRequestIntentEnd): void {
  const key = pullRequestKey(intent.url);
  useSessionPullRequestIntentStore.setState((state) => ({
    bySession: withEntry(state.bySession, sessionId, key, undefined),
    unconfirmed:
      end.status === "unconfirmed" && intent.action === "link"
        ? withEntry(state.unconfirmed, sessionId, key, end.reason)
        : state.unconfirmed,
  }));
  ended(intent.id, end);
}

/**
 * Show `url` linked or unlinked now; a newer intent for the same PR replaces an older one. It
 * lasts until a read of the list settles it, or {@link PULL_REQUEST_INTENT_LIFETIME_MS}.
 */
export function beginPullRequestIntent(
  sessionId: string,
  url: string,
  action: PullRequestIntentAction,
): number {
  const key = pullRequestKey(url);
  const id = nextIntentId++;
  const replaced = useSessionPullRequestIntentStore.getState().bySession[sessionId]?.[key];
  const intent: PullRequestIntent = {
    id,
    url,
    action,
    requestDone: false,
    retried: false,
    unconfirmedReason: action === "link" ? LINK_UNCONFIRMED_REASON : UNLINK_UNCONFIRMED_REASON,
  };
  useSessionPullRequestIntentStore.setState((state) => ({
    bySession: withEntry(state.bySession, sessionId, key, intent),
    unconfirmed: withEntry(state.unconfirmed, sessionId, key, undefined),
  }));
  if (replaced) ended(replaced.id, { status: "replaced" });
  // A request or read that never answers can't hold the overlay up for good.
  deadlines.set(
    id,
    setTimeout(() => {
      const still = current(sessionId, id);
      if (still) drop(sessionId, still, { status: "unconfirmed", reason: unreadableReason(still) });
    }, PULL_REQUEST_INTENT_LIFETIME_MS),
  );
  return id;
}

/**
 * How the intent ends: settled, refused, replaced or unconfirmed. Ask right after beginning it;
 * an intent already gone counts as replaced.
 */
export function pullRequestIntentEnd(sessionId: string, id: number): Promise<PullRequestIntentEnd> {
  if (!current(sessionId, id)) return Promise.resolve({ status: "replaced" });
  return new Promise((resolve) => {
    waiters.set(id, resolve);
  });
}

/** The request was refused: the overlay reverts, and the caller reports why. */
export function failPullRequestIntent(sessionId: string, id: number): void {
  const intent = current(sessionId, id);
  if (intent) drop(sessionId, intent, { status: "refused" });
}

/**
 * The request answered, or can't say how it ended (with what to say if the list never shows
 * it); the server's list decides from here.
 */
export function finishPullRequestIntentRequest(
  sessionId: string,
  id: number,
  unconfirmedReason?: string,
): void {
  const intent = current(sessionId, id);
  if (!intent) return;
  const key = pullRequestKey(intent.url);
  useSessionPullRequestIntentStore.setState((state) => ({
    bySession: withEntry(state.bySession, sessionId, key, {
      ...intent,
      requestDone: true,
      ...(unconfirmedReason ? { unconfirmedReason } : {}),
    }),
  }));
}

/**
 * Compare the session's intents with what a read of its list says it links, or null when the
 * read failed. One the list agrees with clears. Once its request is done, one the list still
 * disagrees with, or a list that can't be read, gets one more read; then it drops and says why.
 * Returns whether to read again.
 */
export function settlePullRequestIntents(
  sessionId: string,
  linked: ReadonlySet<string> | null,
): boolean {
  const intents = useSessionPullRequestIntentStore.getState().bySession[sessionId];
  if (!intents) return false;
  let readAgain = false;
  for (const [key, intent] of Object.entries(intents)) {
    if (linked && linked.has(key) === (intent.action === "link")) {
      drop(sessionId, intent, { status: "settled" });
    } else if (!intent.requestDone) {
      // Its request still runs; the read may have gone out before it landed.
    } else if (!intent.retried) {
      useSessionPullRequestIntentStore.setState((state) => ({
        bySession: withEntry(state.bySession, sessionId, key, { ...intent, retried: true }),
      }));
      readAgain = true;
    } else {
      const reason = linked ? intent.unconfirmedReason : unreadableReason(intent);
      drop(sessionId, intent, { status: "unconfirmed", reason });
    }
  }
  return readAgain;
}

/** Whether the session has intents a read of its list may settle. */
export function hasPullRequestIntents(sessionId: string): boolean {
  return useSessionPullRequestIntentStore.getState().bySession[sessionId] !== undefined;
}

/** Forget every intent, so a test starts clean. */
export function resetPullRequestIntentsForTests(): void {
  for (const timer of deadlines.values()) clearTimeout(timer);
  deadlines.clear();
  waiters.clear();
  useSessionPullRequestIntentStore.setState({ bySession: {}, unconfirmed: {} });
}

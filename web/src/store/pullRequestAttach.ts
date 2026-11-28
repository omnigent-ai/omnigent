import { create } from "zustand";

import { pullRequestKey } from "@/lib/pullRequestReference";
import { PullRequestIntentReplaced } from "@/store/sessionPullRequestIntents";

// One "Link a pull request" picker serves every entry point (the PR panel and
// ⌘K), so they share labels, drafts, and behavior.

export interface AttachedPullRequest {
  sessionId: string;
  url: string;
}

/**
 * A session's latest link request. It lives here rather than in the picker so
 * it survives the picker closing or switching sessions while the host answers.
 */
export type PullRequestAttachAttempt =
  | { id: number; status: "pending"; draft: string; url?: string }
  | {
      id: number;
      status: "failed";
      draft: string;
      message: string;
      url?: string;
      /** The server refused the user, e.g. only the session's owner may link: trying again can't help. */
      refused?: true;
    };

interface PullRequestAttachState {
  /** The session whose picker is open, or null when closed. */
  openSessionId: string | null;
  /** Typed text per session, kept when the picker closes without linking. */
  drafts: Record<string, string>;
  attempts: Record<string, PullRequestAttachAttempt>;
  /** The most recent successful link, so the PR panel can select it. */
  lastAttached: AttachedPullRequest | null;
  open: (sessionId: string) => void;
  close: () => void;
  /** Editing the query clears a failure from an earlier query; a pending link keeps running. */
  setDraft: (sessionId: string, draft: string) => void;
  /** Records a new pending attempt for `url`, or returns null while one is already pending. */
  startAttempt: (sessionId: string, url?: string) => number | null;
  /** Each returns false when the attempt is no longer the session's latest one. */
  finishAttempt: (sessionId: string, attemptId: number, url: string) => boolean;
  failAttempt: (
    sessionId: string,
    attemptId: number,
    message: string,
    options?: { refused?: boolean },
  ) => boolean;
  /** Ends the attempt with nothing to report, keeping the draft and the picker as they are. */
  dropAttempt: (sessionId: string, attemptId: number) => void;
}

let nextAttemptId = 1;

function without<Value>(record: Record<string, Value>, key: string): Record<string, Value> {
  const { [key]: _removed, ...rest } = record;
  return rest;
}

export const usePullRequestAttachStore = create<PullRequestAttachState>((set, get) => ({
  openSessionId: null,
  drafts: {},
  attempts: {},
  lastAttached: null,
  open: (sessionId) => set({ openSessionId: sessionId }),
  close: () => set({ openSessionId: null }),
  setDraft: (sessionId, draft) =>
    set((state) => {
      const attempt = state.attempts[sessionId];
      const staleFailure = attempt?.status === "failed" && attempt.draft !== draft;
      return {
        drafts: { ...state.drafts, [sessionId]: draft },
        attempts: staleFailure ? without(state.attempts, sessionId) : state.attempts,
      };
    }),
  startAttempt: (sessionId, url) => {
    const state = get();
    if (state.attempts[sessionId]?.status === "pending") return null;
    const id = nextAttemptId++;
    const draft = state.drafts[sessionId] ?? "";
    const attempt = { id, status: "pending" as const, draft, ...(url ? { url } : {}) };
    set({ attempts: { ...state.attempts, [sessionId]: attempt } });
    return id;
  },
  // A link can finish after the user moved on: it closes only its own session's
  // picker, and keeps a draft edited while it was pending.
  finishAttempt: (sessionId, attemptId, url) => {
    const state = get();
    const attempt = state.attempts[sessionId];
    if (attempt?.id !== attemptId) return false;
    const draftUnchanged = (state.drafts[sessionId] ?? "") === attempt.draft;
    set({
      attempts: without(state.attempts, sessionId),
      drafts: draftUnchanged ? without(state.drafts, sessionId) : state.drafts,
      lastAttached: { sessionId, url },
      openSessionId: state.openSessionId === sessionId ? null : state.openSessionId,
    });
    return true;
  },
  dropAttempt: (sessionId, attemptId) => {
    const state = get();
    if (state.attempts[sessionId]?.id !== attemptId) return;
    set({ attempts: without(state.attempts, sessionId) });
  },
  // The failure belongs to the current query, so editing it afterwards clears the error.
  failAttempt: (sessionId, attemptId, message, options) => {
    const state = get();
    if (state.attempts[sessionId]?.id !== attemptId) return false;
    const draft = state.drafts[sessionId] ?? "";
    const url = state.attempts[sessionId]?.url;
    set({
      attempts: {
        ...state.attempts,
        [sessionId]: {
          id: attemptId,
          status: "failed",
          draft,
          message,
          ...(url ? { url } : {}),
          ...(options?.refused ? { refused: true as const } : {}),
        },
      },
    });
    return true;
  },
}));

/** What became of one link attempt. */
export type PullRequestLinkOutcome =
  | { status: "linked"; url: string }
  /** This attempt failed, with the message to show. */
  | { status: "failed"; message: string }
  /** Another link for the session was already pending, so this one didn't start. */
  | { status: "busy" }
  /** A newer unlink or link of the same PR took over; it says nothing and goes nowhere. */
  | { status: "overtaken" };

/** Whether a failed link was the server refusing the user (403), not a failure to retry. */
function refusesUser(error: unknown): boolean {
  return (error as { status?: unknown } | null)?.status === 403;
}

/** Links `url` to the session with `attach`, one request per session at a time. */
export async function linkPullRequestAttempt(
  sessionId: string,
  url: string,
  attach: (url: string) => Promise<string | undefined>,
): Promise<PullRequestLinkOutcome> {
  const store = usePullRequestAttachStore.getState();
  const attemptId = store.startAttempt(sessionId, url);
  if (attemptId === null) return { status: "busy" };
  try {
    const linkedUrl = (await attach(url)) ?? url;
    store.finishAttempt(sessionId, attemptId, linkedUrl);
    return { status: "linked", url: linkedUrl };
  } catch (error) {
    if (error instanceof PullRequestIntentReplaced) {
      store.dropAttempt(sessionId, attemptId);
      return { status: "overtaken" };
    }
    const message = error instanceof Error ? error.message : String(error);
    store.failAttempt(sessionId, attemptId, message, { refused: refusesUser(error) });
    return { status: "failed", message };
  }
}

/**
 * Links `url` to the session with `attach`, one request per session at a time.
 * Resolves with the linked URL, or null when a link was already pending, or this request
 * failed or was overtaken.
 */
export async function attachPullRequest(
  sessionId: string,
  url: string,
  attach: (url: string) => Promise<string | undefined>,
): Promise<string | null> {
  const outcome = await linkPullRequestAttempt(sessionId, url, attach);
  return outcome.status === "linked" ? outcome.url : null;
}

/** Where linking one PR to a session stands, as each of its entry points shows it. */
export type PullRequestLinkState =
  | { status: "idle" }
  | { status: "pending" }
  | { status: "failed"; message: string; refused?: true };

const IDLE_LINK: PullRequestLinkState = { status: "idle" };
const PENDING_LINK: PullRequestLinkState = { status: "pending" };

/** The session's latest attempt as it concerns `url`: another PR's attempt leaves it idle. */
export function pullRequestLinkState(
  attempt: PullRequestAttachAttempt | undefined,
  url: string,
): PullRequestLinkState {
  if (!attempt?.url || pullRequestKey(attempt.url) !== pullRequestKey(url)) return IDLE_LINK;
  return attempt.status === "pending"
    ? PENDING_LINK
    : {
        status: "failed",
        message: attempt.message,
        ...(attempt.refused ? { refused: true as const } : {}),
      };
}

/** Opens the link picker for a session; the shared action behind every entry point. */
export function openPullRequestAttach(sessionId: string): void {
  usePullRequestAttachStore.getState().open(sessionId);
}

export const LINK_PULL_REQUEST_LABEL = "Link a PR";
export const LINK_PULL_REQUEST_COMMAND_LABEL = "Link a pull request…";
export const LINK_PULL_REQUEST_KEYWORDS = [
  "attach",
  "pr",
  "pull request",
  "merge request",
  "github",
];

// Refresh one pull request from its header or ⌘K: one attempt at a time per session and pull
// request, and its failure kept on screen until a later refresh of the same one goes through.

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";

import {
  refreshPullRequest,
  type PullRequestRefreshOwner,
  type PullRequestRefreshResult,
} from "./refreshPullRequest";

/** What the header shows of the last refresh that didn't go through. */
export type PullRequestRefreshFailure = { kind: "failed"; message: string } | { kind: "timed-out" };

export interface PullRequestRefresh {
  refresh: () => void;
  refreshing: boolean;
  failure: PullRequestRefreshFailure | null;
}

export const PULL_REQUEST_REFRESH_TIMED_OUT =
  "Refreshing this pull request is taking longer than it should.";

/** The header's line for a failed refresh. */
export function pullRequestRefreshFailureText(failure: PullRequestRefreshFailure): string {
  return failure.kind === "timed-out"
    ? PULL_REQUEST_REFRESH_TIMED_OUT
    : `Couldn’t refresh this pull request: ${failure.message}`;
}

function failureOf(result: PullRequestRefreshResult): PullRequestRefreshFailure | null {
  if (result.kind === "ok") return null;
  if (result.kind === "timed-out") return { kind: "timed-out" };
  return { kind: "failed", message: result.error.message || "Something went wrong." };
}

/** Where a refresh's state belongs: the session and the pull request together. */
const targetKey = (conversationId: string, prUrl: string) =>
  JSON.stringify([conversationId, prUrl]);

interface TargetState {
  /** The attempt running for this target, if any; only its answer may settle it. */
  attempt: symbol | null;
  failure: PullRequestRefreshFailure | null;
}

const IDLE: TargetState = { attempt: null, failure: null };

export function usePullRequestRefresh(
  conversationId: string,
  prUrl: string | null | undefined,
  owner: PullRequestRefreshOwner | null = null,
): PullRequestRefresh {
  const queryClient = useQueryClient();
  const [targets, setTargets] = useState<ReadonlyMap<string, TargetState>>(new Map());
  const latest = useRef(targets);
  latest.current = targets;
  const key = prUrl ? targetKey(conversationId, prUrl) : null;

  const refresh = useCallback(() => {
    if (!key || !prUrl) return;
    // One at a time per target: asking again while it runs waits for the running one.
    if (latest.current.get(key)?.attempt) return;
    const attempt = Symbol("refresh attempt");
    const update = (change: (state: TargetState) => TargetState) =>
      setTargets((current) => {
        const next = new Map(current);
        next.set(key, change(current.get(key) ?? IDLE));
        latest.current = next;
        return next;
      });
    // The failure from before stays on screen until this one goes through.
    update((state) => ({ ...state, attempt }));
    void refreshPullRequest(queryClient, { conversationId, prUrl }, { owner }).then((result) =>
      update((state) =>
        state.attempt === attempt ? { attempt: null, failure: failureOf(result) } : state,
      ),
    );
  }, [conversationId, key, owner, prUrl, queryClient]);

  const state = (key && targets.get(key)) || IDLE;
  return { refresh, refreshing: state.attempt !== null, failure: state.failure };
}

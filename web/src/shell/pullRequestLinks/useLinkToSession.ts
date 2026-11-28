// Linking one PR to a session, as every entry point offers it: whether it can run and why
// not, where a link of it stands, and the link itself. Chat links, ⌘K and the PR's own
// header read the same answer, so they can't disagree.

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useRef, useSyncExternalStore } from "react";

import {
  useAttachSessionPullRequest,
  useSessionHostOffline,
} from "@/hooks/useAttachSessionPullRequest";
import type { PullRequestInfo } from "@/newPullRequestPage/hooks/usePullRequests";
import { useSandboxResume } from "@/hooks/useSandboxResume";
import { useSessionPullRequests } from "@/newPullRequestPage/hooks/useSessionPullRequests";
import { useWorkspaceAsleep } from "@/hooks/useWorkspaceChangedFiles";
import { pullRequestKey } from "@/lib/pullRequestReference";
import {
  linksPullRequest,
  sessionPullRequestMembership,
  sessionPullRequestsFull,
} from "@/lib/sessionPullRequestMembership";
import { HIDDEN, type CommandAvailability } from "@/store/paletteCommands";
import {
  linkPullRequestAttempt,
  type PullRequestLinkState,
  pullRequestLinkState,
  usePullRequestAttachStore,
} from "@/store/pullRequestAttach";
import { useSessionPullRequestIntentStore } from "@/store/sessionPullRequestIntents";

import {
  linkNeedsHostReason,
  useLinkPullRequestAvailability,
} from "../linkPullRequestAvailability";
import { linkToSessionAvailability } from "./model";

export interface LinkToSession {
  /** Whether the session links the PR now, by the server's own list once it has one. */
  linked: boolean;
  availability: CommandAvailability;
  /** This PR's own link, while it runs or after it failed; another PR's leaves it idle. */
  state: PullRequestLinkState;
  /** Link it now, with no picker; does nothing while it can't run. */
  link: () => void;
}

const IDLE: PullRequestLinkState = { status: "idle" };

/** The session's live info as already loaded; reads the cache only, so it never asks the host. */
function useCachedSessionInfo(sessionId: string): PullRequestInfo | undefined {
  const queryClient = useQueryClient();
  const subscribe = useCallback(
    (listener: () => void) => queryClient.getQueryCache().subscribe(listener),
    [queryClient],
  );
  return useSyncExternalStore(subscribe, () =>
    queryClient.getQueryData<PullRequestInfo>(["pr-page-github-info", sessionId]),
  );
}

/** Linking any of a session's PRs: one PR's answer at a time, for a list that offers it per row. */
export interface SessionPullRequestLinker {
  linkedOf: (url: string) => boolean;
  availabilityOf: (url: string) => CommandAvailability;
  stateOf: (url: string) => PullRequestLinkState;
  /** Link it now, with no picker; does nothing while it can't run. */
  link: (url: string) => void;
}

/**
 * @param readsStoredList - False where another view already reads the session's stored list, so
 *   this one only follows it: mounting a second reader would read a failed list again.
 */
export function useSessionPullRequestLinker(
  sessionId: string,
  { readsStoredList = true }: { readsStoredList?: boolean } = {},
): SessionPullRequestLinker {
  const access = useLinkPullRequestAvailability(sessionId);
  const storedRead = useSessionPullRequests(sessionId, undefined, { enabled: readsStoredList });
  // An older server keeps no list; the session's live info, if loaded, has it.
  const live = useCachedSessionInfo(sessionId)?.prs;
  // A link or unlink asked for here shows at once, before the server's list does.
  const intents = useSessionPullRequestIntentStore((store) => store.bySession[sessionId]);
  const membership = useMemo(
    () => sessionPullRequestMembership({ stored: storedRead.data, live, intents }),
    [storedRead.data, live, intents],
  );
  const unconfirmed = useSessionPullRequestIntentStore((store) => store.unconfirmed[sessionId]);
  const attempt = usePullRequestAttachStore((store) => store.attempts[sessionId]);
  const hostOffline = useSessionHostOffline(sessionId, { error: null });
  const sandboxAsleep = useWorkspaceAsleep(sessionId);
  const sandboxResuming = useSandboxResume(sessionId).status === "resuming";
  const attach = useAttachSessionPullRequest(sessionId);
  const latestAttach = useRef(attach.mutateAsync);
  latestAttach.current = attach.mutateAsync;

  const linkedOf = useCallback((url: string) => linksPullRequest(membership, url), [membership]);
  const stateOf = useCallback(
    (url: string): PullRequestLinkState => {
      const own = pullRequestLinkState(attempt, url);
      // A link the server's list never showed reverts, saying why.
      const reverted = unconfirmed?.[pullRequestKey(url)];
      return own.status === "idle" && reverted ? { status: "failed", message: reverted } : own;
    },
    [attempt, unconfirmed],
  );
  const availabilityOf = useCallback(
    (url: string) =>
      linkToSessionAvailability({
        access,
        linked: linkedOf(url),
        full: sessionPullRequestsFull(membership, url),
        pending: attempt?.status === "pending",
        hostUnavailableReason: hostOffline
          ? linkNeedsHostReason(sandboxAsleep, sandboxResuming)
          : null,
      }),
    [access, linkedOf, membership, attempt?.status, hostOffline, sandboxAsleep, sandboxResuming],
  );
  const availableRef = useRef(availabilityOf);
  availableRef.current = availabilityOf;
  const link = useCallback(
    (url: string) => {
      if (!availableRef.current(url).available) return;
      // The attempt lives in the store, so its state outlasts this view.
      void linkPullRequestAttempt(sessionId, url, (target) => latestAttach.current(target));
    },
    [sessionId],
  );
  return useMemo(
    () => ({ linkedOf, availabilityOf, stateOf, link }),
    [linkedOf, availabilityOf, stateOf, link],
  );
}

export function useLinkToSession(sessionId: string, url: string | null): LinkToSession {
  const linker = useSessionPullRequestLinker(sessionId);
  const linked = url !== null && linker.linkedOf(url);
  const state = useMemo(() => (url === null ? IDLE : linker.stateOf(url)), [linker, url]);
  const availability = useMemo(
    () => (url === null ? HIDDEN : linker.availabilityOf(url)),
    [linker, url],
  );
  const link = useCallback(() => {
    if (url !== null) linker.link(url);
  }, [linker, url]);
  return { linked, availability, state, link };
}

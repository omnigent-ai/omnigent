// Links asked for from a chat link's menu or ⌘K. A link can outlive the menu that
// asked for it and a switch to another session, so one host runs each request
// with the session it was made in.

import { useEffect, useSyncExternalStore } from "react";
import { create } from "zustand";

import type { CodeHostPullRequest } from "@/lib/codeHosts";

import type { PullRequestsTarget, ShowPullRequestsOptions } from "../FileViewerContext";
import { createFocusedTarget } from "../focusedTarget";

export interface PullRequestLinkRequest {
  key: number;
  /** The session whose chat showed the link. */
  sessionId: string;
  url: string;
}

let nextKey = 1;

export const usePullRequestLinkRequests = create<{ request: PullRequestLinkRequest | null }>(
  () => ({ request: null }),
);

/** Ask the host to link `url` to session `sessionId`. */
export function requestPullRequestLink(sessionId: string, url: string): void {
  usePullRequestLinkRequests.setState({ request: { key: nextKey++, sessionId, url } });
}

/** Take `request` off the queue as it starts, so a host mounted later never runs it again. */
export function takePullRequestLinkRequest(request: PullRequestLinkRequest): void {
  usePullRequestLinkRequests.setState((state) =>
    state.request?.key === request.key ? { request: null } : state,
  );
}

/** The chat PR link keyboard focus is in, which ⌘K offers actions for. */
export interface FocusedPullRequestLink {
  sessionId: string;
  url: string;
  /** The PR as the chat recognized it, where the URL alone wouldn't say, as on an Enterprise host. */
  pullRequest?: CodeHostPullRequest;
}

const focusedLink = createFocusedTarget<FocusedPullRequestLink>();

/** Spread the returned handler as a chat PR link's `onFocus`, so ⌘K acts on it. */
export const useFocusedPullRequestLinkSource = focusedLink.useFocusSource;

/** The focused chat PR link, while it's still on the page. */
export const useFocusedPullRequestLink = focusedLink.useFocused;

export const focusedPullRequestLinkStore = focusedLink.useStore;

type ShowPullRequests = (target: PullRequestsTarget, options: ShowPullRequestsOptions) => void;

/**
 * Each session's on-screen chats, by the way they show its pull requests: the
 * routed chat's, or a grid pane's. A chat that leaves the screen takes its out.
 */
const tabOpeners = new Map<string, Set<{ openTab: ShowPullRequests; showsTabs: boolean }>>();
// Told whenever a chat's opener comes or goes, so what a session's chat shows stays live.
const openerListeners = new Set<() => void>();
const notifyOpeners = () => {
  for (const listener of [...openerListeners]) listener();
};
const subscribeToOpeners = (listener: () => void) => {
  openerListeners.add(listener);
  return () => {
    openerListeners.delete(listener);
  };
};

/**
 * Let a chat showing `sessionId` show its pull requests when a link it asked for
 * finishes. `showsTabs`: its workspace sidebar shows each pull request in its own tab.
 */
export function useRegisterPullRequestTabOpener(
  sessionId: string | undefined,
  openTab: ShowPullRequests | null,
  showsTabs = false,
): void {
  useEffect(() => {
    if (!sessionId || !openTab) return;
    // Every link in a chat registers the same opener; each keeps its own entry, so one
    // link leaving doesn't take the opener from the others.
    const registration = { openTab, showsTabs };
    const openers = tabOpeners.get(sessionId) ?? new Set();
    openers.add(registration);
    tabOpeners.set(sessionId, openers);
    notifyOpeners();
    return () => {
      openers.delete(registration);
      if (openers.size === 0 && tabOpeners.get(sessionId) === openers) tabOpeners.delete(sessionId);
      notifyOpeners();
    };
  }, [sessionId, openTab, showsTabs]);
}

/** Whether the chat on screen for `sessionId` shows each pull request in its own tab. */
export function showsPullRequestTabsFor(sessionId: string): boolean {
  const openers = tabOpeners.get(sessionId);
  return (openers ? [...openers].at(-1) : undefined)?.showsTabs ?? false;
}

/** `showsPullRequestTabsFor`, kept current as chats come, go or switch sidebar design. */
export function useShowsPullRequestTabsFor(sessionId: string): boolean {
  return useSyncExternalStore(subscribeToOpeners, () => showsPullRequestTabsFor(sessionId));
}

/** Show `url` in its own tab of the sidebar of the chat showing `sessionId`; false where none can. */
export function openChatPullRequestInSidebar(sessionId: string, url: string): boolean {
  if (!showsPullRequestTabsFor(sessionId)) return false;
  return openPullRequestTabFor(sessionId, { kind: "pull", url }, { classic: "stay" });
}

/** Show `target` in a chat still showing `sessionId`; false when none is on screen. */
export function openPullRequestTabFor(
  sessionId: string,
  target: PullRequestsTarget,
  options: ShowPullRequestsOptions,
): boolean {
  const openers = tabOpeners.get(sessionId);
  const latest = openers ? [...openers].at(-1) : undefined;
  latest?.openTab(target, options);
  return latest !== undefined;
}

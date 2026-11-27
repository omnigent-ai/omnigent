// Every pull request call this Mac's shell runs goes through here: reads, changes, accounts and the
// links kept on this Mac. After the page's server or API base path changes, this reads as a shell
// with none of those bridges, so each caller takes its path without one until the page reloads.

import { useSyncExternalStore } from "react";

import {
  authorizeDesktopCodeHostRun as bridgeAuthorizeRun,
  cancelDesktopCodeHostWrite as bridgeCancelWrite,
  desktopBranchPullRequestChoices as bridgeBranchChoices,
  desktopCodeHostQuery as bridgeCodeHostQuery,
  desktopCodeHostWrite as bridgeCodeHostWrite,
  desktopCodeHostWriteAs as bridgeCodeHostWriteAs,
  desktopLocalPullRequestLinks as bridgeLocalLinks,
  desktopMacAccounts as bridgeMacAccounts,
  endDesktopCodeHostRun as bridgeEndRun,
  githubFastTrackQuery as bridgeFastTrackQuery,
  hasCodeHostAccountsBridge as bridgeHasAccounts,
  hasCodeHostBridge as bridgeHasCodeHost,
  hasCodeHostReadCancel as bridgeHasReadCancel,
  hasCodeHostWriteBridge as bridgeHasWrite,
  hasGitHubFastTrackBridge as bridgeHasFastTrack,
  hasLocalPullRequestLinksBridge as bridgeHasLocalLinks,
  hasNamedAccountEvents as bridgeHasNamedAccountEvents,
  hasNamedCodeHostWriteBridge as bridgeHasNamedWrite,
  hasRunGrantBridge as bridgeHasRunGrant,
  onDesktopMacAccountsChanged as bridgeOnMacAccountsChanged,
  onDesktopNamedAccountsChanged as bridgeOnNamedAccountsChanged,
  setDesktopMacAccount as bridgeSetMacAccount,
} from "@/lib/nativeBridge";
import type {
  BranchPullRequestChoice,
  CodeHostQueryRequest,
  CodeHostRunAsRequest,
  CodeHostRunAuthorization,
  CodeHostRunRequest,
  CodeHostWriteAsRequest,
  CodeHostWriteRequest,
  CodeHostWriteResult,
  GitHubFastTrackAccount,
  GitHubFastTrackResult,
  LocalPullRequestLink,
  LocalPullRequestLinksRequest,
  MacAccountChoice,
  MacAccountIntent,
} from "@/lib/nativeBridge";
import { onReadPathServerMoved, readPathServerMoved } from "@/lib/readPathServer";

const open = () => !readPathServerMoved();

const UNSUPPORTED = {
  ok: false as const,
  error: {
    code: "unsupported" as const,
    message: "This page reads pull requests through its session.",
  },
};

/** `call`'s answer, or `closed` when the server moved before it was sent or while it ran. */
async function whileOpen<Answer>(call: () => Promise<Answer>, closed: Answer): Promise<Answer> {
  if (!open()) return closed;
  const answer = await call();
  // The server moved while the shell answered: the answer was for the page's first server.
  return open() ? answer : closed;
}

export const hasCodeHostBridge = () => open() && bridgeHasCodeHost();
export const hasCodeHostReadCancel = () => open() && bridgeHasReadCancel();
export const hasCodeHostAccountsBridge = () => open() && bridgeHasAccounts();
export const hasNamedAccountEvents = () => open() && bridgeHasNamedAccountEvents();
export const hasCodeHostWriteBridge = () => open() && bridgeHasWrite();
export const hasNamedCodeHostWriteBridge = () => open() && bridgeHasNamedWrite();
export const hasGitHubFastTrackBridge = () => open() && bridgeHasFastTrack();
export const hasLocalPullRequestLinksBridge = () => open() && bridgeHasLocalLinks();
export const hasRunGrantBridge = (...args: Parameters<typeof bridgeHasRunGrant>) =>
  open() && bridgeHasRunGrant(...args);

/** One code-host read in the shell; `unsupported`, never sent, once the server moved. */
export function desktopCodeHostQuery<Data>(
  request: CodeHostQueryRequest,
  signal?: AbortSignal,
): Promise<GitHubFastTrackResult<Data>> {
  return whileOpen(() => bridgeCodeHostQuery<Data>(request, signal), UNSUPPORTED);
}

/** One fast-track read in the shell; `unsupported`, never sent, once the server moved. */
export function githubFastTrackQuery<Data>(
  operation: string,
  account: GitHubFastTrackAccount,
  params?: unknown,
): Promise<GitHubFastTrackResult<Data>> {
  return whileOpen(() => bridgeFastTrackQuery<Data>(operation, account, params), UNSUPPORTED);
}

/** `host`'s account choice on this Mac; null, as a shell that can't say, once the server moved. */
export function desktopMacAccounts(host: string): Promise<MacAccountChoice | null> {
  return whileOpen(() => bridgeMacAccounts(host), null);
}

/** Save this Mac's account for `host`; refused, as a shell that can't, once the server moved. */
export function setDesktopMacAccount(
  host: string,
  intent: MacAccountIntent,
): Promise<MacAccountChoice | null> {
  return whileOpen(() => bridgeSetMacAccount(host, intent), null);
}

/** The links kept on this Mac; none, as a shell without the store, once the server moved. */
export function desktopLocalPullRequestLinks(
  request: LocalPullRequestLinksRequest,
): Promise<LocalPullRequestLink[] | null> {
  return whileOpen(() => bridgeLocalLinks(request), null);
}

/** A branch's pull request choices on this Mac; none once the server moved. */
export function desktopBranchPullRequestChoices(
  request: Parameters<typeof bridgeBranchChoices>[0],
): Promise<BranchPullRequestChoice[] | null> {
  return whileOpen(() => bridgeBranchChoices(request), null);
}

// A change sent before the server moved keeps its answer: it was made on the code host either way.
/** One pull request change on this Mac; `unsupported`, never sent, once the server moved. */
export function desktopCodeHostWrite<Data>(
  request: CodeHostWriteRequest,
): Promise<CodeHostWriteResult<Data>> {
  return open() ? bridgeCodeHostWrite<Data>(request) : Promise.resolve(UNSUPPORTED);
}

/** One change as a login read by name; `unsupported`, never sent, once the server moved. */
export function desktopCodeHostWriteAs<Data>(
  request: CodeHostWriteAsRequest,
): Promise<CodeHostWriteResult<Data>> {
  return open() ? bridgeCodeHostWriteAs<Data>(request) : Promise.resolve(UNSUPPORTED);
}

/** Authorize a run of changes; refused, never asked, once the server moved. */
export function authorizeDesktopCodeHostRun(
  request: CodeHostRunRequest | CodeHostRunAsRequest,
): Promise<CodeHostRunAuthorization> {
  return open() ? bridgeAuthorizeRun(request) : Promise.resolve({ ok: false, code: "unsupported" });
}

// Letting go of a change or run always reaches the shell, so nothing started before a move is held.
export function cancelDesktopCodeHostWrite(requestId: string): void {
  bridgeCancelWrite(requestId);
}

export function endDesktopCodeHostRun(requestId: string): void {
  bridgeEndRun(requestId);
}

/** This Mac's account changes, until the server moves; returns the stop. */
export function onDesktopMacAccountsChanged(
  callback: Parameters<typeof bridgeOnMacAccountsChanged>[0],
): () => void {
  return bridgeOnMacAccountsChanged((...args) => {
    if (open()) callback(...args);
  });
}

/** Changes to logins read by name on this Mac, until the server moves; returns the stop. */
export function onDesktopNamedAccountsChanged(
  callback: Parameters<typeof bridgeOnNamedAccountsChanged>[0],
): () => void {
  return bridgeOnNamedAccountsChanged((...args) => {
    if (open()) callback(...args);
  });
}

const subscribe = (listener: () => void) => onReadPathServerMoved(listener);

/** Whether this page may still call this Mac's shell, re-rendering once its server moves. */
export function useMacBridgeOpen(): boolean {
  return useSyncExternalStore(subscribe, open, open);
}

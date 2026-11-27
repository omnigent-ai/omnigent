// Every pull request read this Mac's shell runs goes through here. After the page's server or API
// base path changes, this reads as a shell with no code-host or fast-track bridge, so each caller
// takes the path it takes without one: the session's host, until the page reloads.

import { useSyncExternalStore } from "react";

import {
  desktopCodeHostQuery as bridgeCodeHostQuery,
  desktopMacAccounts as bridgeMacAccounts,
  githubFastTrackQuery as bridgeFastTrackQuery,
  hasCodeHostAccountsBridge as bridgeHasAccounts,
  hasCodeHostBridge as bridgeHasCodeHost,
  hasCodeHostReadCancel as bridgeHasReadCancel,
  hasGitHubFastTrackBridge as bridgeHasFastTrack,
  hasNamedAccountEvents as bridgeHasNamedAccountEvents,
  hasNamedCodeHostWriteBridge as bridgeHasNamedWrite,
  type CodeHostQueryRequest,
  type GitHubFastTrackAccount,
  type GitHubFastTrackResult,
  type MacAccountChoice,
} from "@/lib/nativeBridge";
import { onReadPathServerMoved, readPathServerMoved } from "@/lib/readPathServer";

const open = () => !readPathServerMoved();

function unsupported<Data>(): GitHubFastTrackResult<Data> {
  return {
    ok: false,
    error: { code: "unsupported", message: "This page reads pull requests through its session." },
  };
}

export const hasCodeHostBridge = () => open() && bridgeHasCodeHost();
export const hasCodeHostReadCancel = () => open() && bridgeHasReadCancel();
export const hasCodeHostAccountsBridge = () => open() && bridgeHasAccounts();
export const hasNamedAccountEvents = () => open() && bridgeHasNamedAccountEvents();
export const hasNamedCodeHostWriteBridge = () => open() && bridgeHasNamedWrite();
export const hasGitHubFastTrackBridge = () => open() && bridgeHasFastTrack();

/** One code-host read in the shell; `unsupported`, never sent, once the server moved. */
export async function desktopCodeHostQuery<Data>(
  request: CodeHostQueryRequest,
  signal?: AbortSignal,
): Promise<GitHubFastTrackResult<Data>> {
  if (!open()) return unsupported();
  const result = await bridgeCodeHostQuery<Data>(request, signal);
  // The server moved while the shell read: the answer was for the page's first server.
  return open() ? result : unsupported();
}

/** One fast-track read in the shell; `unsupported`, never sent, once the server moved. */
export async function githubFastTrackQuery<Data>(
  operation: string,
  account: GitHubFastTrackAccount,
  params?: unknown,
): Promise<GitHubFastTrackResult<Data>> {
  if (!open()) return unsupported();
  const result = await bridgeFastTrackQuery<Data>(operation, account, params);
  return open() ? result : unsupported();
}

/** `host`'s account choice on this Mac; null, as a shell that can't say, once the server moved. */
export async function desktopMacAccounts(host: string): Promise<MacAccountChoice | null> {
  if (!open()) return null;
  const choice = await bridgeMacAccounts(host);
  return open() ? choice : null;
}

const subscribe = (listener: () => void) => onReadPathServerMoved(listener);

/** Whether this page may still read on this Mac, re-rendering once its server moves. */
export function useMacBridgeOpen(): boolean {
  return useSyncExternalStore(subscribe, open, open);
}

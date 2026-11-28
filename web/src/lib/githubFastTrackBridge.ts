// The only way to the shell's GitHub fast track. After the page's server or API base path
// changes, this reads as a shell without the fast track until the page reloads.

import { useSyncExternalStore } from "react";

import {
  githubFastTrackQuery as bridgeFastTrackQuery,
  hasGitHubFastTrackBridge as bridgeHasFastTrack,
  type GitHubFastTrackAccount,
  type GitHubFastTrackResult,
} from "@/lib/nativeBridge";
import {
  githubFastTrackServerMoved,
  onGitHubFastTrackServerMoved,
} from "@/lib/githubFastTrackServer";

const open = () => !githubFastTrackServerMoved();

const UNSUPPORTED = {
  ok: false as const,
  error: {
    code: "unsupported" as const,
    message: "The GitHub fast track isn't available on this page.",
  },
};

/** Whether this shell offers the fast track to this page, until its server moves. */
export const hasGitHubFastTrackBridge = () => open() && bridgeHasFastTrack();

/** One fast-track read; `unsupported`, never sent, once the server moved, and dropped if it moved meanwhile. */
export async function githubFastTrackQuery<Data>(
  operation: string,
  account: GitHubFastTrackAccount,
  params?: unknown,
): Promise<GitHubFastTrackResult<Data>> {
  if (!open()) return UNSUPPORTED;
  const answer = await bridgeFastTrackQuery<Data>(operation, account, params);
  return open() ? answer : UNSUPPORTED;
}

/** Whether this page may still use the fast track, re-rendering once its server moves. */
export function useGitHubFastTrackOpen(): boolean {
  return useSyncExternalStore(onGitHubFastTrackServerMoved, open, open);
}

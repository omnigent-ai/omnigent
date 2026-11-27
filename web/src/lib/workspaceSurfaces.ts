// Which pull request design a device shows. The new page and list (`pull_requests_v2`) show in
// the tabs sidebar (`workspace_sidebar_v2`) only; elsewhere, or with the flag off, the classic
// pull request panel and page show. The app shell reports the design for code outside React.

import { useEffect } from "react";

import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useTabsSidebar } from "@/shell/useTabsSidebar";

export type WorkspaceSurfacesDesign = "tabs" | "classic";

let accepted: WorkspaceSurfacesDesign | null = null;
const listeners = new Set<() => void>();

/** The pull request design the app shell has on screen, or null before it reports one. */
export function acceptedWorkspaceDesign(): WorkspaceSurfacesDesign | null {
  return accepted;
}

/** The app shell reports the pull request design it shows. */
export function setAcceptedWorkspaceDesign(design: WorkspaceSurfacesDesign | null): void {
  if (accepted === design) return;
  accepted = design;
  for (const listener of [...listeners]) listener();
}

/** Call `listener` when the accepted design changes. */
export function subscribeAcceptedWorkspaceDesign(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Whether the new pull request page and list show: the tabs sidebar with `pull_requests_v2`, live. */
export function usePullRequestsV2(): boolean {
  const serverInfo = useServerInfo();
  const tabsSidebar = useTabsSidebar();
  return tabsSidebar && isFeatureEnabled(serverInfo, "pull_requests_v2");
}

/** The app shell reports the design it shows, so reads outside React follow it. */
export function useReportPullRequestsDesign(): void {
  const pullRequestsV2 = usePullRequestsV2();
  useEffect(() => {
    setAcceptedWorkspaceDesign(pullRequestsV2 ? "tabs" : "classic");
  }, [pullRequestsV2]);
}

/** Forget the accepted design, so a test starts clean; subscribers stay, as module stores'. */
export function resetAcceptedWorkspaceDesignForTests(): void {
  accepted = null;
}

// What the new page adds around the pull request details: its actions' route tooltips and its
// image scope. Each is kept on every tab and in every early state, since none waits for the body.

import { type ReactNode, useId } from "react";

import {
  SecondaryReadPriorityProvider,
  secondaryReadPriority,
} from "@/hooks/secondaryReadPriority";

import { PullRequestImageScope } from "./PullRequestPageImage";
import { LifecycleSurfaceContext } from "./pullRequestLifecycleActions";

export function PullRequestPageScope({
  enabled,
  conversationId,
  prUrl,
  changesShown = false,
  children,
}: {
  enabled: boolean;
  conversationId: string;
  prUrl: string | null | undefined;
  /** False while the panel is hidden, so its ⌘K rows leave with it. */
  active: boolean;
  /** Changes shows, so the page's secondary reads wait behind its patch. */
  changesShown?: boolean;
  children: ReactNode;
}) {
  // This pane's page: a confirmation opened here shows here alone.
  const surface = useId();
  // One column, the same with the flag on or off so a flip keeps the details mounted: the
  // status takes its line, and the details take the rest without spilling past the pane.
  return (
    <SecondaryReadPriorityProvider value={secondaryReadPriority(changesShown)}>
      <LifecycleSurfaceContext.Provider value={enabled ? surface : null}>
        <div className="flex h-full min-h-0 flex-col">
          <div className="min-h-0 flex-1">
            {enabled && prUrl ? (
              <PullRequestImageScope conversationId={conversationId} prUrl={prUrl}>
                {children}
              </PullRequestImageScope>
            ) : (
              children
            )}
          </div>
        </div>
      </LifecycleSurfaceContext.Provider>
    </SecondaryReadPriorityProvider>
  );
}

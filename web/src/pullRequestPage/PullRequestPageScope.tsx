// What the new page adds around the pull request details: its actions' route tooltips, the
// ⌘K owner for where the selected PR is read, and its status line. Each is kept on every tab
// and in every early state, stored and offline included, since none waits for the body.

import { type ReactNode, useId } from "react";

import { ActionRouteHintsContext } from "@/lib/actionRouteHint";

import {
  SecondaryReadPriorityProvider,
  secondaryReadPriority,
} from "@/hooks/secondaryReadPriority";
import { useWorkspaceServeable } from "@/hooks/useWorkspaceChangedFiles";

import { PullRequestImageScope } from "./PullRequestPageImage";
import { ReadSourceOwner } from "./ReadSourceOwner";
import { ReadSourceStatus } from "./ReadSourceStatus";
import { LifecycleSurfaceContext } from "./pullRequestLifecycleActions";

export function PullRequestPageScope({
  enabled,
  conversationId,
  prUrl,
  active,
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
  // Each session's own machine: two sessions showing one PR can differ.
  const serveable = useWorkspaceServeable(conversationId);
  // This pane's page: a confirmation opened here shows here alone.
  const surface = useId();
  // One column, the same with the flag on or off so a flip keeps the details mounted: the
  // status takes its line, and the details take the rest without spilling past the pane.
  return (
    <SecondaryReadPriorityProvider value={secondaryReadPriority(changesShown)}>
      <ActionRouteHintsContext.Provider value={enabled}>
        <LifecycleSurfaceContext.Provider value={enabled ? surface : null}>
          {enabled && prUrl && <ReadSourceOwner prUrl={prUrl} active={active} />}
          <div className="flex h-full min-h-0 flex-col">
            {enabled && prUrl && (
              <ReadSourceStatus prUrl={prUrl} hostCanAnswer={serveable !== false} />
            )}
            <div className="min-h-0 flex-1">
              {enabled && prUrl ? (
                <PullRequestImageScope prUrl={prUrl}>{children}</PullRequestImageScope>
              ) : (
                children
              )}
            </div>
          </div>
        </LifecycleSurfaceContext.Provider>
      </ActionRouteHintsContext.Provider>
    </SecondaryReadPriorityProvider>
  );
}

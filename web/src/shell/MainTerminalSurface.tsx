// The session page's terminal surface: the panes view where it shows, else the Terminal view as
// it always was.

import { type ComponentProps, lazy, Suspense } from "react";

import { RenderErrorBoundary } from "@/components/RenderErrorBoundary";

import { MainTerminalView } from "./MainTerminalView";
import { usePanesView } from "./panesViewGate";
import { PublishedTerminalView } from "./PublishedTerminalView";
import { useFloatingPanesShown } from "./useFloatingPanes";

// The panes view loads only where it shows, so its code stays out of the session page's bundle
// with workspace_sidebar_v2 off.
const SplitPanesMainView = lazy(() =>
  import("./SplitPanesMainView").then((module) => ({ default: module.SplitPanesMainView })),
);

export interface MainTerminalSurfaceProps extends ComponentProps<typeof MainTerminalView> {
  /** The session shows Chat, so a split beside it shows the web chat as the agent's pane. */
  chatShown?: boolean;
}

export function MainTerminalSurface({ chatShown, splittable, ...props }: MainTerminalSurfaceProps) {
  const panesView = usePanesView();
  // With floating panes the session's panes are cards on the sidebar's color.
  const floatingPanes = useFloatingPanesShown();
  if (panesView) {
    // Nothing shows while it loads, so no terminal attaches here before the panes take it. If
    // the panes view fails to render, the session keeps its chat and gets the Terminal view.
    return (
      <RenderErrorBoundary
        name="the terminal panes"
        resetKey={props.conversationId}
        fallback={<PublishedTerminalView {...props} splittable={splittable} />}
      >
        <Suspense fallback={null}>
          <SplitPanesMainView {...props} chatShown={chatShown} cards={floatingPanes} />
        </Suspense>
      </RenderErrorBoundary>
    );
  }
  return <MainTerminalView {...props} splittable={splittable} />;
}

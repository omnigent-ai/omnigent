import { useCallback, useEffect, type KeyboardEvent } from "react";
import { PageScroll } from "@/components/PageScroll";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { isKeyOwningOverlayOpen } from "@/lib/hotkeys";
import { useParams, useReplaceSearchParams, useSearchParams } from "@/lib/routing";
import {
  useProjectBoardSearchStore,
  useProjectComposerStore,
} from "@/projectHome/ProjectHomeHeader";
import {
  PROJECT_PANEL_SESSION_PARAM,
  projectSessionPanelSearch,
} from "@/projectHome/projectHomeNavigation";
import { ProjectKanbanBoard } from "@/projectHome/ProjectKanbanBoard";
import { ProjectSessionPanel } from "@/projectHome/ProjectSessionPanel";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";

const SESSION_PANEL_DEFAULT_WIDTH_VW = 40;
const SESSION_PANEL_MIN_WIDTH_PX = 360;

/**
 * A project's home: its sessions board, with the header's composer floating over
 * it and a card's quick view open in a side panel.
 */
export function ProjectHomePage() {
  const { projectName = "" } = useParams<{ projectName: string }>();
  const isMobile = useIsMobileViewport();
  const composerOpen = useProjectComposerStore((state) => state.open) && !isMobile;
  const setComposerOpen = useProjectComposerStore((state) => state.setOpen);
  useEffect(() => () => setComposerOpen(false), [setComposerOpen]);
  const searchQuery = useProjectBoardSearchStore((state) => state.query);
  const setSearchQuery = useProjectBoardSearchStore((state) => state.setQuery);
  // Each project starts unfiltered.
  useEffect(() => () => setSearchQuery(""), [projectName, setSearchQuery]);

  // Phones have no room beside the board; their cards open the full session.
  const [searchParams] = useSearchParams();
  const panelSessionId = isMobile ? null : searchParams.get(PROJECT_PANEL_SESSION_PARAM);
  const replaceSearchParams = useReplaceSearchParams();
  const closePanel = useCallback(() => {
    const next = new URLSearchParams(searchParams);
    next.delete(PROJECT_PANEL_SESSION_PARAM);
    replaceSearchParams(next);
  }, [searchParams, replaceSearchParams]);
  const { panelWidth, handleProps } = useResizablePanel(
    panelSessionId !== null,
    SESSION_PANEL_DEFAULT_WIDTH_VW,
    SESSION_PANEL_MIN_WIDTH_PX,
    "projectSessionPanelWidthPx",
  );
  useEffect(() => {
    if (panelSessionId === null) return;
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !event.defaultPrevented && !isKeyOwningOverlayOpen()) {
        closePanel();
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [panelSessionId, closePanel]);

  function closeComposerOnEscape(event: KeyboardEvent<HTMLDivElement>) {
    // Portaled menus bubble through React too; they handle their own Escape.
    if (
      event.key === "Escape" &&
      !event.defaultPrevented &&
      event.currentTarget.contains(event.target as Node)
    ) {
      // Handled here, so an open session panel stays.
      event.preventDefault();
      setComposerOpen(false);
    }
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Only the lanes and the panel scroll; the page stays put. */}
      <PageScroll
        className="overflow-hidden"
        maxWidthClassName="max-w-none"
        contentClassName="flex h-full"
        // Clears the taller header the project title gets on desktop.
        extraTop={isMobile ? "0.75rem" : "4rem"}
        extraBottom="0px"
      >
        <ProjectKanbanBoard
          projectName={projectName}
          searchQuery={searchQuery}
          quickViewHref={isMobile ? undefined : projectSessionPanelSearch}
          selectedSessionId={panelSessionId}
          onCloseQuickView={closePanel}
          overlayClearance={composerOpen ? "10rem" : undefined}
        />
        {panelSessionId !== null && (
          <aside
            aria-label="Session quick view"
            // It closes on Escape, so page-wide Escape shortcuts leave keys inside it alone.
            data-escape-owner=""
            className="relative flex shrink-0 flex-col pt-1 pr-4 pb-4"
            style={{ width: panelWidth }}
          >
            {/* The hook's gutter padding is laid out for the push panels' seam handle; this
                handle overhangs the panel's edge instead, so keep its own box and claim touch. */}
            <div
              {...handleProps}
              style={{ touchAction: "none" }}
              aria-label="Resize session panel"
              className="group/resize absolute inset-y-0 -left-2 z-10 flex w-2 cursor-col-resize items-center justify-center outline-none"
            >
              <span className="h-9 w-0.5 rounded-full bg-muted-foreground/30 opacity-0 group-hover/resize:opacity-100 group-focus-visible/resize:opacity-100" />
            </div>
            <ProjectSessionPanel sessionId={panelSessionId} onClose={closePanel} />
          </aside>
        )}
      </PageScroll>
      {composerOpen && (
        <div
          className="pointer-events-none absolute inset-x-0 bottom-0 z-20 flex justify-center px-4 pb-6"
          // Centered over the board, clear of the session panel.
          style={{ right: panelSessionId !== null ? panelWidth : undefined }}
        >
          <div
            data-testid="project-floating-composer"
            // drop-shadow() takes no spread, so this can't use the spread-based shadow tokens.
            className="project-floating-composer pointer-events-auto w-full max-w-3xl [filter:drop-shadow(0_16px_48px_rgb(var(--ui-shadow-neutral-rgb)/0.2))]"
            onKeyDown={closeComposerOnEscape}
          >
            <NewChatLandingScreen
              projectName={projectName}
              onSessionStarted={() => setComposerOpen(false)}
            />
          </div>
        </div>
      )}
    </div>
  );
}

import { useCallback, useEffect, useState, type KeyboardEvent, type ReactNode } from "react";
import { PageScroll } from "@/components/PageScroll";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { isKeyOwningOverlayOpen } from "@/lib/hotkeys";
import { useNavigate, useParams, useReplaceSearchParams, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import {
  useProjectBoardSearchStore,
  useProjectComposerStore,
} from "@/projectHome/ProjectHomeHeader";
import {
  PROJECT_PANEL_SESSION_PARAM,
  projectView,
  projectViewSearch,
  ticketPanelSearch,
  ticketSelectionIds,
  type ProjectView,
} from "@/projectHome/projectHomeNavigation";
import { ProjectKanbanBoard } from "@/projectHome/ProjectKanbanBoard";
import { ProjectSessionPanel } from "@/projectHome/ProjectSessionPanel";
import { ProjectTicketBoard } from "@/projectHome/ProjectTicketBoard";
import { ProjectTicketPanel } from "@/projectHome/ProjectTicketPanel";
import { ProjectChat } from "@/projectHome/ProjectChat";
import { ProjectViewNavigation } from "@/projectHome/ProjectViewNavigation";
import { useProjectTicketsEnabled } from "@/projectHome/useProjectTickets";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";

const SESSION_PANEL_DEFAULT_WIDTH_VW = 40;
const SESSION_PANEL_MIN_WIDTH_PX = 360;

/**
 * Ticket projects share one conversation and board; legacy projects keep their sessions board.
 */
export function ProjectHomePage() {
  const { projectName = "" } = useParams<{ projectName: string }>();
  const isMobile = useIsMobileViewport();
  const tickets = useProjectTicketsEnabled();
  const composerOpen = useProjectComposerStore((state) => state.open) && !isMobile;
  const setComposerOpen = useProjectComposerStore((state) => state.setOpen);
  useEffect(() => () => setComposerOpen(false), [setComposerOpen]);
  const searchQuery = useProjectBoardSearchStore((state) => state.query);
  const setSearchQuery = useProjectBoardSearchStore((state) => state.setQuery);
  // Each project starts unfiltered.
  useEffect(() => () => setSearchQuery(""), [projectName, setSearchQuery]);

  // Saved session-panel links redirect to the normal session on every viewport.
  const [searchParams] = useSearchParams();
  const view = projectView(searchParams);
  const panelSessionId = searchParams.get(PROJECT_PANEL_SESSION_PARAM);
  const panelTicketId = tickets ? searchParams.get("ticket") : null;
  const hasPanel = panelSessionId !== null || panelTicketId !== null;
  const [ticketExpanded, setTicketExpanded] = useState(false);
  const ticketChatOpen = tickets && view === "chat" && hasPanel;
  const ticketFillsPage = panelTicketId !== null && ticketExpanded && !isMobile;
  const hideBoard = (isMobile && hasPanel) || ticketFillsPage || ticketChatOpen;
  useEffect(() => setTicketExpanded(false), [projectName, panelTicketId]);
  const replaceSearchParams = useReplaceSearchParams();
  const closePanel = useCallback(() => {
    setTicketExpanded(false);
    const next = new URLSearchParams(searchParams);
    next.delete(PROJECT_PANEL_SESSION_PARAM);
    next.delete("ticket");
    next.delete("orchestrator");
    next.delete("agent");
    if (tickets) next.set("view", view);
    replaceSearchParams(next);
  }, [searchParams, replaceSearchParams, tickets, view]);
  const { panelWidth, handleProps } = useResizablePanel(
    hasPanel,
    SESSION_PANEL_DEFAULT_WIDTH_VW,
    SESSION_PANEL_MIN_WIDTH_PX,
    "projectSessionPanelWidthPx",
  );
  useEffect(() => {
    if (!hasPanel) return;
    function closeOnEscape(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !event.defaultPrevented && !isKeyOwningOverlayOpen()) {
        event.preventDefault();
        if (ticketFillsPage) setTicketExpanded(false);
        else closePanel();
      }
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [hasPanel, closePanel, ticketFillsPage]);

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

  if (panelSessionId) return <ProjectSessionPanel sessionId={panelSessionId} />;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {/* Only the lanes and the panel scroll; the page stays put. */}
      <PageScroll
        className="overflow-hidden"
        maxWidthClassName="max-w-none"
        contentClassName="flex h-full"
        // Matches the compact project title row above the view tabs.
        extraTop={isMobile ? "0.25rem" : "0.5rem"}
        extraBottom="0px"
      >
        <ProjectViews enabled={tickets} projectName={projectName} view={view}>
          {tickets ? (
            <div
              hidden={hideBoard}
              className={hideBoard ? "hidden" : "flex min-h-0 min-w-0 flex-1"}
            >
              {view === "chat" ? (
                !ticketChatOpen && <ProjectChat key={projectName} projectName={projectName} />
              ) : (
                <ProjectTicketBoard
                  projectName={projectName}
                  selectedTicketId={panelTicketId}
                  ticketIds={ticketSelectionIds(searchParams)}
                  overlayClearance={composerOpen ? "10rem" : undefined}
                />
              )}
            </div>
          ) : (
            <ProjectKanbanBoard
              projectName={projectName}
              searchQuery={searchQuery}
              selectedSessionId={panelSessionId}
              onCloseQuickView={closePanel}
              overlayClearance={composerOpen ? "10rem" : undefined}
            />
          )}
          {hasPanel && (
            <aside
              aria-label={panelTicketId ? "Ticket details" : "Session quick view"}
              // It closes on Escape, so page-wide Escape shortcuts leave keys inside it alone.
              data-escape-owner=""
              className={cn(
                "relative flex min-h-0 min-w-0 shrink-0 flex-col pt-1 pr-4 pb-4",
                ticketFillsPage && "pl-4",
              )}
              style={{ width: isMobile || ticketFillsPage || ticketChatOpen ? "100%" : panelWidth }}
            >
              {!isMobile && !ticketFillsPage && !ticketChatOpen && (
                <div
                  {...handleProps}
                  aria-label="Resize session panel"
                  className="group/resize absolute inset-y-0 -left-2 z-10 flex w-2 cursor-col-resize items-center justify-center outline-none"
                >
                  <span className="h-9 w-0.5 rounded-full bg-muted-foreground/30 opacity-0 group-hover/resize:opacity-100 group-focus-visible/resize:opacity-100" />
                </div>
              )}
              {panelTicketId !== null && (
                <ProjectTicketPanel
                  key={`${projectName}:${panelTicketId}`}
                  projectName={projectName}
                  ticketId={panelTicketId}
                  onClose={closePanel}
                  onCreated={(id) =>
                    replaceSearchParams(new URLSearchParams(ticketPanelSearch(id, searchParams)))
                  }
                  expanded={ticketFillsPage}
                  onExpandedChange={isMobile ? undefined : setTicketExpanded}
                />
              )}
            </aside>
          )}
        </ProjectViews>
      </PageScroll>
      {composerOpen && (
        <div
          className="pointer-events-none absolute inset-x-0 bottom-0 z-20 flex justify-center px-4 pb-6"
          // Centered over the board, clear of the session panel.
          style={{ right: hasPanel && !ticketFillsPage ? panelWidth : undefined }}
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

function ProjectViews({
  enabled,
  projectName,
  view,
  children,
}: {
  enabled: boolean;
  projectName: string;
  view: ProjectView;
  children: ReactNode;
}) {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  if (!enabled) return children;
  return (
    <Tabs
      value={view}
      onValueChange={(value) => navigate(projectViewSearch(value as ProjectView, search))}
      className="min-h-0 min-w-0 flex-1 gap-0"
    >
      <ProjectViewNavigation projectName={projectName} view={view} />
      <TabsContent value={view} className="flex min-h-0 min-w-0 pt-1">
        {children}
      </TabsContent>
    </Tabs>
  );
}

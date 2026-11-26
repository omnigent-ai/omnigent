import { useCallback, useEffect, useState, type KeyboardEvent } from "react";
import { PageScroll } from "@/components/PageScroll";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useIsPhoneViewport } from "@/hooks/useIsPhoneViewport";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { isKeyOwningOverlayOpen } from "@/lib/hotkeys";
import { useNavigate, useParams, useReplaceSearchParams, useSearchParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import {
  projectNewSessionPath,
  useProjectBoardSearchStore,
  useProjectComposerStore,
} from "@/projectHome/ProjectHomeHeader";
import {
  PROJECT_PANEL_PERSONA_PARAM,
  PROJECT_PANEL_SESSION_PARAM,
  PROJECT_RULES_TAB,
  PROJECT_SUMMARY_TAB,
  type ProjectHomeView,
  projectHomeView,
  PROJECT_TAB_PARAM,
  isLegacyOrchestratorLink,
  projectHomePath,
  ticketPanelSearch,
  ticketSelectionIds,
} from "@/projectHome/projectHomeNavigation";
import { ProjectKanbanBoard } from "@/projectHome/ProjectKanbanBoard";
import { ProjectSessionPanel } from "@/projectHome/ProjectSessionPanel";
import { ProjectTicketBoard } from "@/projectHome/ProjectTicketBoard";
import { useMarkProjectTicketsSeen, useNewTicketsOnOpen } from "@/projectHome/projectTicketsSeen";
import { ProjectTicketPanel } from "@/projectHome/ProjectTicketPanel";
import {
  ProjectOrchestrator,
  ProjectPersonaChat,
  teamMemberCharter,
} from "@/projectHome/ProjectOrchestrator";
import { useProjectIdentities } from "@/projectHome/useProjectIdentities";
import { ProjectFileViewerHost } from "@/projectHome/ProjectFileViewer";
import { ProjectBoardActions, ProjectViewTools } from "@/projectHome/ProjectBoardActions";
import { ProjectRules } from "@/projectHome/ProjectRules";
import { ProjectHomeTabs, ProjectSummary } from "@/projectHome/ProjectSummary";
import {
  TicketBoardRequesterFilter,
  useBoardRequesterFilter,
} from "@/projectHome/TicketBoardRequesterFilter";
import { useProjectTicketsEnabled, useTicketProject } from "@/projectHome/useProjectTickets";
import {
  NEEDS_YOU_PARAM,
  NEEDS_YOU_VALUE,
  useNeedsYouTicketIds,
} from "@/projectHome/needsYouFilter";
import { NewChatLandingScreen } from "@/shell/NewChatDialog";
import { OutdatedHostBanner } from "@/projectHome/OutdatedHostBanner";
import { useProjectAgentSession } from "@/projectHome/useProjectAgentSession";

const SESSION_PANEL_DEFAULT_WIDTH_VW = 40;
const SESSION_PANEL_MIN_WIDTH_PX = 360;

/**
 * Tickets are the project home; the saved orchestrator can open beside them.
 */
export function ProjectHomePage({ orchestrator = false }: { orchestrator?: boolean }) {
  const { projectName = "" } = useParams<{ projectName: string }>();
  const navigate = useNavigate();
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
  const panelSessionId = searchParams.get(PROJECT_PANEL_SESSION_PARAM);
  const panelTicketId = tickets ? searchParams.get("ticket") : null;
  const orchestratorOpen = tickets && orchestrator && panelTicketId === null;
  // A persona's session opened from the header shares the Lead's side chat.
  const personaSessionId =
    tickets && !orchestratorOpen && panelTicketId === null
      ? searchParams.get(PROJECT_PANEL_PERSONA_PARAM)
      : null;
  const hasPanel =
    panelSessionId !== null ||
    panelTicketId !== null ||
    orchestratorOpen ||
    personaSessionId !== null;
  const [ticketExpanded, setTicketExpanded] = useState(false);
  const ticketFillsPage = panelTicketId !== null && ticketExpanded && !isMobile;
  const hideBoard = (isMobile && hasPanel) || ticketFillsPage;
  useEffect(() => setTicketExpanded(false), [projectName, panelTicketId]);
  const replaceSearchParams = useReplaceSearchParams();
  const { projectId } = useTicketProject(projectName);
  const persona = useProjectIdentities(personaSessionId ? projectId : null).data?.find(
    (identity) => identity.active_session_id === personaSessionId,
  );
  const personaName = persona?.name ?? "Team member";
  const isNewTicket = useNewTicketsOnOpen(projectId, panelTicketId);
  useMarkProjectTicketsSeen(projectId, tickets && !hideBoard);
  const closePanel = useCallback(() => {
    setTicketExpanded(false);
    const next = new URLSearchParams(searchParams);
    next.delete(PROJECT_PANEL_SESSION_PARAM);
    next.delete(PROJECT_PANEL_PERSONA_PARAM);
    next.delete("ticket");
    next.delete("orchestrator");
    next.delete("agent");
    next.delete("view");
    if (orchestrator) {
      navigate(`${projectHomePath(projectName)}${next.size ? `?${next}` : ""}`, { replace: true });
    } else replaceSearchParams(next);
  }, [searchParams, replaceSearchParams, orchestrator, navigate, projectName]);
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

  const [boardMine, setBoardMine] = useBoardRequesterFilter(projectId ?? null);
  const needsYouIds = useNeedsYouTicketIds(tickets ? (projectId ?? null) : null);
  const leadSessionId = useProjectAgentSession(tickets ? (projectId ?? null) : null).sessionId;
  // With nothing waiting on the viewer the chip hides and the board shows its usual view.
  const needsYou = searchParams.get(NEEDS_YOU_PARAM) === NEEDS_YOU_VALUE && needsYouIds.length > 0;
  const setNeedsYou = (on: boolean) => {
    const next = new URLSearchParams(searchParams);
    next.delete("tickets");
    if (on) next.set(NEEDS_YOU_PARAM, NEEDS_YOU_VALUE);
    else next.delete(NEEDS_YOU_PARAM);
    replaceSearchParams(next);
  };
  const view: ProjectHomeView = tickets && !orchestrator ? projectHomeView(searchParams) : "board";
  // Summary and Rules replace the board, so they drop its ticket and panel selection.
  const showTab = (nextView: ProjectHomeView) => {
    const next = nextView === "board" ? new URLSearchParams(searchParams) : new URLSearchParams();
    if (nextView === "summary") next.set(PROJECT_TAB_PARAM, PROJECT_SUMMARY_TAB);
    else if (nextView === "rules") next.set(PROJECT_TAB_PARAM, PROJECT_RULES_TAB);
    else next.delete(PROJECT_TAB_PARAM);
    navigate(`${projectHomePath(projectName)}${next.size ? `?${next}` : ""}`);
  };

  // Phones keep these tools in the top bar.
  const viewTools = isMobile ? undefined : <ProjectViewTools projectName={projectName} />;
  // Phones hold the project's actions in a labelled row of their own, above the tabs.
  const isPhone = useIsPhoneViewport();
  const phoneActions =
    tickets && isPhone ? (
      <ProjectBoardActions
        projectName={projectName}
        newSessionPath={projectNewSessionPath(projectName)}
        phoneRow
      />
    ) : null;

  if (panelSessionId) return <ProjectSessionPanel sessionId={panelSessionId} />;
  if (tickets && !orchestrator && !panelTicketId && isLegacyOrchestratorLink(searchParams))
    return (
      <PageScroll contentClassName="flex min-h-full" maxWidthClassName="max-w-none">
        <ProjectOrchestrator key={projectName} projectName={projectName} />
      </PageScroll>
    );

  if (view !== "board")
    return (
      <div className="relative flex min-h-0 flex-1 flex-col">
        <ProjectFileViewerHost projectId={projectId} />
        <PageScroll maxWidthClassName="max-w-none" extraTop={isMobile ? "0.25rem" : "0.5rem"}>
          {phoneActions}
          <ProjectHomeTabs view={view} onChange={showTab} end={viewTools} />
          {view === "summary" ? (
            <ProjectSummary key={projectName} projectName={projectName} />
          ) : (
            <ProjectRules key={projectName} projectName={projectName} />
          )}
        </PageScroll>
      </div>
    );

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <ProjectFileViewerHost projectId={projectId} />
      {/* Only the lanes and the panel scroll; the page stays put. */}
      <PageScroll
        className="overflow-hidden"
        maxWidthClassName="max-w-none"
        contentClassName="flex h-full flex-col"
        // Matches the compact project title row.
        extraTop={isMobile ? "0.25rem" : "0.5rem"}
        extraBottom="0px"
      >
        {!ticketFillsPage && phoneActions}
        {tickets && !ticketFillsPage && (
          <ProjectHomeTabs
            view="board"
            onChange={showTab}
            end={viewTools}
            trailing={
              <TicketBoardRequesterFilter
                mine={boardMine}
                onChange={(mine) => {
                  if (needsYou) setNeedsYou(false);
                  setBoardMine(mine);
                }}
                needsYou={{
                  count: needsYouIds.length,
                  active: needsYou,
                  onSelect: () => setNeedsYou(true),
                }}
                compact
              />
            }
          />
        )}
        {tickets && !ticketFillsPage && (
          <OutdatedHostBanner sessionId={leadSessionId} name="the Lead" className="mx-4 mb-2" />
        )}
        <div className="flex min-h-0 flex-1">
          {tickets ? (
            <div
              hidden={hideBoard}
              className={hideBoard ? "hidden" : "flex min-h-0 min-w-0 flex-1"}
            >
              <ProjectTicketBoard
                projectName={projectName}
                selectedTicketId={panelTicketId}
                isNewTicket={isNewTicket}
                ticketIds={needsYou ? needsYouIds : ticketSelectionIds(searchParams)}
                requesterFilterInHeader={!ticketFillsPage}
                overlayClearance={composerOpen ? "10rem" : undefined}
              />
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
              aria-label={
                orchestratorOpen
                  ? "Lead side chat"
                  : personaSessionId
                    ? `${personaName} side chat`
                    : "Ticket details"
              }
              // It closes on Escape, so page-wide Escape shortcuts leave keys inside it alone.
              data-escape-owner=""
              className={cn(
                "relative flex min-h-0 min-w-0 shrink-0 flex-col pt-1 pr-4 pb-4",
                ticketFillsPage && "pl-4",
              )}
              style={{ width: isMobile || ticketFillsPage ? "100%" : panelWidth }}
            >
              {!isMobile && !ticketFillsPage && (
                <div
                  {...handleProps}
                  aria-label="Resize session panel"
                  className="group/resize absolute inset-y-0 -left-2 z-10 flex w-2 cursor-col-resize items-center justify-center outline-none"
                >
                  <span className="h-9 w-0.5 rounded-full bg-muted-foreground/30 opacity-0 group-hover/resize:opacity-100 group-focus-visible/resize:opacity-100" />
                </div>
              )}
              {orchestratorOpen ? (
                <ProjectOrchestrator
                  key={projectName}
                  projectName={projectName}
                  onClose={closePanel}
                />
              ) : personaSessionId ? (
                <ProjectPersonaChat
                  key={personaSessionId}
                  sessionId={personaSessionId}
                  name={personaName}
                  charter={teamMemberCharter(persona?.instructions)}
                  onClose={closePanel}
                />
              ) : (
                panelTicketId !== null && (
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
                )
              )}
            </aside>
          )}
        </div>
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

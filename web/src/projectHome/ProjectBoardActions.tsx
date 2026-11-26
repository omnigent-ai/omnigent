import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { create } from "zustand";
import { ChevronDownIcon, MessageCirclePlusIcon, PlusIcon } from "lucide-react";
import { PersonaAvatar } from "@/components/PersonaAvatar";
import { Button } from "@/components/ui/button";
import { ButtonGroup } from "@/components/ui/button-group";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useNavigate, useSearchParams } from "@/lib/routing";
import { ProjectCostBadge } from "./ProjectCost";
import { ProjectFiles } from "./ProjectFiles";
import { PROJECT_FILE_PARAM } from "./ProjectFileViewer";
import { ProjectIdentityRoster } from "./ProjectIdentityRoster";
import { ProjectTicketSources } from "./ProjectTicketSources";
import { projectOrchestratorPath, ticketPanelSearch } from "./projectHomeNavigation";
import { useProjectRecord } from "./useProjectAgentSession";
import { useProjectIdentities } from "./useProjectIdentities";
import { useTicketProject } from "./useProjectTickets";
import { useClickThroughDialog } from "./useClickThroughDialog";

/**
 * The top bar's project actions. Desktop shows the team and a New ticket button
 * whose menu starts a session; phones show icon buttons, with Sources and Files
 * here because their page has no room for the view toolbar's tools.
 */
export function ProjectBoardActions({
  projectName,
  newSessionPath,
  children,
  phoneRow = false,
}: {
  projectName: string;
  newSessionPath: string;
  /** Phone-only extra buttons, before New ticket. */
  children?: ReactNode;
  /** The phone page's own action row: a labelled Team entry, the tools and a labelled New ticket. */
  phoneRow?: boolean;
}) {
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const { projectId, projects } = useTicketProject(projectName);
  const leadRemoved = useProjectRecord(projectId).data?.lead_removed;
  const rosterReady = !projects.isLoading && !projects.isError && projectId !== null;
  const leadPath = leadRemoved ? undefined : projectOrchestratorPath(projectName, search);
  // The Lead is drawn like every other persona, by its own name.
  const leadName =
    useProjectIdentities(projectId).data?.find((identity) => identity.builtin)?.name ?? "Lead";
  // New ticket also closes an open project file.
  const newTicketSearch = new URLSearchParams(search);
  newTicketSearch.delete(PROJECT_FILE_PARAM);
  const newTicket = ticketPanelSearch("new", newTicketSearch);
  // A modal dialog's overlay covers these buttons; pressing one closes it, then acts.
  const newTicketRef = useRef<HTMLAnchorElement>(null);
  useClickThroughDialog(newTicketRef, () => navigate(newTicket));
  const toolsElsewhere = useViewToolsHostStore((state) => state.hosts > 0);
  if (phoneRow)
    return (
      <div
        // Named like the top bar so the tools drop their labels as they do there.
        className="@container/topbar mx-4 mb-2 flex shrink-0 items-center gap-1"
        role="group"
        aria-label="Project actions"
      >
        {rosterReady ? (
          <ProjectIdentityRoster
            projectId={projectId}
            projectName={projectName}
            leadPath={leadPath}
            labelled
          />
        ) : (
          leadPath !== undefined && (
            <Button asChild variant="ghost" size="sm">
              <Link to={leadPath}>
                <PersonaAvatar name={leadName} size="xs" />
                {leadName}
              </Link>
            </Button>
          )
        )}
        <div className="ml-auto flex items-center gap-1">
          {!projects.isLoading && !projects.isError && (
            <>
              <ProjectTicketSources projectId={projectId} projectName={projectName} />
              <ProjectFiles projectId={projectId} />
            </>
          )}
          <NewTicketButtons newTicket={newTicket} newSessionPath={newSessionPath} touch />
        </div>
      </div>
    );
  return (
    <div
      className="flex shrink-0 items-center gap-1 md:gap-3"
      role="group"
      aria-label="Project actions"
    >
      {/* Until the roster loads, or for a label-only folder, the Lead stands alone. */}
      {leadPath !== undefined && !rosterReady && (
        <Button asChild variant="ghost" size={isMobile ? "icon" : "sm"}>
          <Link to={leadPath} aria-label="Lead">
            <PersonaAvatar name={leadName} size="xs" />
            {!isMobile && <span className="max-w-28 truncate">{leadName}</span>}
          </Link>
        </Button>
      )}
      {rosterReady && (
        <ProjectIdentityRoster
          projectId={projectId}
          projectName={projectName}
          leadPath={leadPath}
        />
      )}
      {isMobile ? (
        <>
          {!projects.isLoading && !projects.isError && (
            <>
              <ProjectTicketSources projectId={projectId} projectName={projectName} />
              <ProjectFiles projectId={projectId} />
            </>
          )}
          {children}
          <Button asChild size="icon">
            <Link ref={newTicketRef} to={newTicket} aria-label="New ticket">
              <PlusIcon />
            </Link>
          </Button>
        </>
      ) : (
        <>
          {!toolsElsewhere && <ProjectViewTools projectName={projectName} host={false} />}
          <NewTicketButtons newTicket={newTicket} newSessionPath={newSessionPath} />
        </>
      )}
    </div>
  );
}

/** New ticket, with a menu of other ways to start; a covered button still works through a dialog. */
function NewTicketButtons({
  newTicket,
  newSessionPath,
  touch = false,
}: {
  newTicket: string;
  newSessionPath: string;
  /** Phones: New ticket matches the menu half's touch height. */
  touch?: boolean;
}) {
  const navigate = useNavigate();
  const newTicketRef = useRef<HTMLAnchorElement>(null);
  const createMenuRef = useRef<HTMLButtonElement>(null);
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  useClickThroughDialog(newTicketRef, () => navigate(newTicket));
  useClickThroughDialog(createMenuRef, () => setCreateMenuOpen(true));
  return (
    <ButtonGroup aria-label="Create">
      <Button asChild className={touch ? "h-10" : undefined}>
        <Link ref={newTicketRef} to={newTicket}>
          <PlusIcon />
          New ticket
        </Link>
      </Button>
      <DropdownMenu open={createMenuOpen} onOpenChange={setCreateMenuOpen}>
        <DropdownMenuTrigger asChild>
          <Button
            ref={createMenuRef}
            size="icon"
            aria-label="More ways to start"
            data-testid="project-create-menu"
            // A hairline in the button's own text color separates the two halves.
            className="border-l! border-primary-foreground/20"
          >
            <ChevronDownIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem
            className="gap-2"
            data-testid="project-new-session-button"
            onSelect={() => navigate(newSessionPath)}
          >
            <MessageCirclePlusIcon className="size-4" />
            New session
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </ButtonGroup>
  );
}

interface ViewToolsHostState {
  hosts: number;
  setHosts: (update: (hosts: number) => number) => void;
}

/** How many view toolbars hold the tools; with none (a full-page ticket) the top bar keeps them. */
const useViewToolsHostStore = create<ViewToolsHostState>((set) => ({
  hosts: 0,
  setHosts: (update) => set((state) => ({ hosts: update(state.hosts) })),
}));

/** The week's cost, ticket sources and files: the view toolbar's right end on desktop. */
export function ProjectViewTools({
  projectName,
  host = true,
}: {
  projectName: string;
  /** The toolbar's copy; the top bar's fallback copy passes false. */
  host?: boolean;
}) {
  const { projectId, projects } = useTicketProject(projectName);
  const setHosts = useViewToolsHostStore((state) => state.setHosts);
  // Before paint, so the tools never show in both places.
  useLayoutEffect(() => {
    if (!host) return;
    setHosts((hosts) => hosts + 1);
    return () => setHosts((hosts) => hosts - 1);
  }, [host, setHosts]);
  if (projects.isLoading || projects.isError) return null;
  return (
    <>
      <div className="shrink-0 @max-[52rem]/topbar:hidden">
        <ProjectCostBadge projectId={projectId} plain />
      </div>
      <span aria-hidden className="mx-1 h-4 w-px bg-border @max-[52rem]/topbar:hidden" />
      <ProjectTicketSources projectId={projectId} projectName={projectName} />
      <ProjectFiles projectId={projectId} />
    </>
  );
}

import { useLayoutEffect, type ReactNode } from "react";
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
import { ProjectIdentityRoster } from "./ProjectIdentityRoster";
import { ProjectTicketSources } from "./ProjectTicketSources";
import { projectOrchestratorPath, ticketPanelSearch } from "./projectHomeNavigation";
import { useProjectRecord } from "./useProjectAgentSession";
import { useProjectIdentities } from "./useProjectIdentities";
import { useTicketProject } from "./useProjectTickets";

/**
 * The top bar's project actions. Desktop shows the team and a New ticket button
 * whose menu starts a session; phones show icon buttons, with Sources and Files
 * here because their page has no room for the view toolbar's tools.
 */
export function ProjectBoardActions({
  projectName,
  newSessionPath,
  children,
}: {
  projectName: string;
  newSessionPath: string;
  /** Phone-only extra buttons, before New ticket. */
  children?: ReactNode;
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
  const newTicket = ticketPanelSearch("new", search);
  const toolsElsewhere = useViewToolsHostStore((state) => state.hosts > 0);
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
            <Link to={newTicket} aria-label="New ticket">
              <PlusIcon />
            </Link>
          </Button>
        </>
      ) : (
        <>
          {!toolsElsewhere && <ProjectViewTools projectName={projectName} host={false} />}
          <ButtonGroup aria-label="Create">
            <Button asChild>
              <Link to={newTicket}>
                <PlusIcon />
                New ticket
              </Link>
            </Button>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
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
        </>
      )}
    </div>
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

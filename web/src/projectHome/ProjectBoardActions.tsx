import { MessageSquareIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useSearchParams } from "@/lib/routing";
import { ProjectFiles } from "./ProjectFiles";
import { ProjectIdentityRoster } from "./ProjectIdentityRoster";
import { ProjectTicketSources } from "./ProjectTicketSources";
import { projectOrchestratorPath, ticketPanelSearch } from "./projectHomeNavigation";
import { useProjectRecord } from "./useProjectAgentSession";
import { useTicketProject } from "./useProjectTickets";

export function ProjectBoardActions({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const [search] = useSearchParams();
  const { projectId, projects } = useTicketProject(projectName);
  const leadRemoved = useProjectRecord(projectId).data?.lead_removed;
  const rosterReady = !projects.isLoading && !projects.isError && projectId !== null;
  return (
    <div
      className="flex shrink-0 items-center gap-1 md:gap-2"
      role="group"
      aria-label="Project actions"
    >
      {!leadRemoved && !(isMobile && rosterReady) && (
        <Button asChild variant="ghost" size={isMobile ? "icon" : "sm"}>
          <Link to={projectOrchestratorPath(projectName, search)} aria-label="Lead">
            <MessageSquareIcon />
            {!isMobile && <span className="max-xl:hidden">Lead</span>}
          </Link>
        </Button>
      )}
      {rosterReady && (
        <ProjectIdentityRoster
          projectId={projectId}
          projectName={projectName}
          // Phones fold the Lead into the Team menu to keep the header on screen.
          leadPath={leadRemoved ? undefined : projectOrchestratorPath(projectName, search)}
        />
      )}
      {!projects.isLoading && !projects.isError && (
        <ProjectTicketSources projectId={projectId} projectName={projectName} />
      )}
      {!projects.isLoading && !projects.isError && <ProjectFiles projectId={projectId} />}
      <Button asChild size={isMobile ? "icon" : "default"}>
        <Link to={ticketPanelSearch("new", search)} aria-label="New ticket">
          <PlusIcon />
          {!isMobile && "New ticket"}
        </Link>
      </Button>
    </div>
  );
}

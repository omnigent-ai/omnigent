import type { ReactNode } from "react";
import { MessageSquareIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useSearchParams } from "@/lib/routing";
import { ProjectFiles } from "./ProjectFiles";
import { ProjectIdentityRoster } from "./ProjectIdentityRoster";
import { ProjectTicketSources } from "./ProjectTicketSources";
import { projectOrchestratorPath, ticketPanelSearch } from "./projectHomeNavigation";
import { useTicketProject } from "./useProjectTickets";

export function ProjectBoardActions({
  projectName,
  children,
}: {
  projectName: string;
  children: ReactNode;
}) {
  const isMobile = useIsMobileViewport();
  const [search] = useSearchParams();
  const { projectId, projects } = useTicketProject(projectName);
  return (
    <div className="flex items-center gap-2" role="group" aria-label="Project actions">
      <Button asChild variant="ghost" size={isMobile ? "icon" : "sm"}>
        <Link to={projectOrchestratorPath(projectName, search)} aria-label="Orchestrator">
          <MessageSquareIcon />
          {!isMobile && "Orchestrator"}
        </Link>
      </Button>
      {!projects.isLoading && !projects.isError && (
        <ProjectIdentityRoster projectId={projectId} projectName={projectName} />
      )}
      {!projects.isLoading && !projects.isError && (
        <ProjectTicketSources projectId={projectId} projectName={projectName} />
      )}
      {!projects.isLoading && !projects.isError && <ProjectFiles projectId={projectId} />}
      {children}
      <Button asChild size={isMobile ? "icon" : "default"}>
        <Link to={ticketPanelSearch("new", search)} aria-label="New ticket">
          <PlusIcon />
          {!isMobile && "New ticket"}
        </Link>
      </Button>
    </div>
  );
}

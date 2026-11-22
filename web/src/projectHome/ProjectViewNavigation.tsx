import { useRef, useState } from "react";
import { Columns3Icon, MessageSquareIcon, MoreVerticalIcon, PlusIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useSearchParams } from "@/lib/routing";
import { ProjectTicketSources } from "./ProjectTicketSources";
import { ProjectTicketStagesDialog } from "./ProjectTicketStagesDialog";
import { ticketPanelSearch, type ProjectView } from "./projectHomeNavigation";
import { useTicketProject, useTicketStagesEnabled, useTicketSummary } from "./useProjectTickets";

export function ProjectViewNavigation({
  projectName,
  view,
}: {
  projectName: string;
  view: ProjectView;
}) {
  const isMobile = useIsMobileViewport();
  const [search] = useSearchParams();
  const { projectId, projects } = useTicketProject(projectName);
  const stagesEnabled = useTicketStagesEnabled();
  const summary = useTicketSummary(projectId);
  const count = summary.isError ? undefined : summary.data?.needs_response_count;
  const incomplete = !summary.isError && summary.data?.is_complete === false;
  return (
    <div className="flex min-h-12 shrink-0 flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-border/60 px-4 pb-2">
      <div className="flex items-center gap-1">
        <TabsList variant="line" aria-label="Project views" className="gap-5">
          <TabsTrigger value="chat">
            <MessageSquareIcon />
            Chat
          </TabsTrigger>
          <TabsTrigger value="board">
            <Columns3Icon />
            Board
            {((count ?? 0) > 0 || incomplete) && (
              <span
                aria-label={
                  incomplete
                    ? count
                      ? `At least ${count} ${count === 1 ? "ticket needs" : "tickets need"} a response`
                      : "Response count is incomplete"
                    : `${count} ${count === 1 ? "ticket needs" : "tickets need"} a response`
                }
                title={incomplete ? "Some tickets have not been counted." : "Needs response"}
                className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-primary-cta px-1 text-sm text-primary-cta-foreground tabular-nums"
              >
                {count ? count : ""}
                {incomplete ? "+" : ""}
              </span>
            )}
          </TabsTrigger>
        </TabsList>
        {view === "board" && stagesEnabled && (
          <BoardMenu key={projectName} projectName={projectName} />
        )}
      </div>
      {view === "board" && (
        <div className="flex items-center gap-2" role="group" aria-label="Ticket actions">
          {!projects.isLoading && !projects.isError && (
            <ProjectTicketSources projectId={projectId} projectName={projectName} />
          )}
          <Button asChild size={isMobile ? "icon" : "sm"}>
            <Link to={ticketPanelSearch("new", search)} aria-label="New ticket">
              <PlusIcon />
              {!isMobile && "New ticket"}
            </Link>
          </Button>
        </div>
      )}
    </div>
  );
}

function BoardMenu({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const [editing, setEditing] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  return (
    <>
      <DropdownMenu modal={!isMobile}>
        <DropdownMenuTrigger asChild>
          <Button
            ref={trigger}
            variant="ghost"
            size="icon-sm"
            aria-label="Board actions"
            className="text-muted-foreground"
          >
            <MoreVerticalIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          onCloseAutoFocus={(event) => {
            if (editing) event.preventDefault();
          }}
        >
          <DropdownMenuItem onSelect={() => setEditing(true)}>
            <Columns3Icon />
            Edit stages
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {editing && (
        <ProjectTicketStagesDialog
          projectName={projectName}
          onClose={() => setEditing(false)}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            trigger.current?.focus();
          }}
        />
      )}
    </>
  );
}

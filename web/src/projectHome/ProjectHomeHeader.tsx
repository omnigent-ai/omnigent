import {
  MessageCirclePlusIcon,
  MoreHorizontalIcon,
  PencilIcon,
  Settings2Icon,
  Trash2Icon,
} from "lucide-react";
import { create } from "zustand";
import { ProjectLandingIcon } from "@/components/ProjectIconPicker";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useProjectConfig, useProjects } from "@/hooks/useConversations";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { Link, useNavigate } from "@/lib/routing";
import { useProjectFolderMenu } from "@/shell/Sidebar";
import { projectHomePath } from "./projectHomeNavigation";

interface ProjectComposerState {
  open: boolean;
  setOpen: (open: boolean) => void;
}

/** Whether the project home's floating composer shows; the header button and the page share it. */
export const useProjectComposerStore = create<ProjectComposerState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));

/** The project home's header title: its editable icon tile and name. */
export function ProjectHomeTitle({ projectName }: { projectName: string }) {
  const { data: projects, isLoading: projectsLoading } = useProjects();
  const projectId = projects?.find((project) => project.name === projectName)?.id ?? null;
  const { data: config } = useProjectConfig(projectId);
  return (
    <div className="flex min-w-0 items-center gap-3">
      <ProjectLandingIcon
        projectId={projectId}
        projectName={projectName}
        config={config}
        // The PATCH replaces the whole config, so editing waits until it loads.
        // A label-only folder (`projectId === null`) has no config to lose.
        configReady={!projectsLoading && (projectId === null || config !== undefined)}
        small
      />
      <h1 className="min-w-0 truncate text-xl font-medium text-foreground">{projectName}</h1>
      <ProjectHomeActions projectName={projectName} projectId={projectId} icon={config?.icon} />
    </div>
  );
}

/** Project settings, plus an overflow menu to rename or delete the project. */
function ProjectHomeActions({
  projectName,
  projectId,
  icon,
}: {
  projectName: string;
  projectId: string | null;
  icon: string | undefined;
}) {
  const navigate = useNavigate();
  const { actions, dialogs } = useProjectFolderMenu(
    projectName,
    projectId,
    icon,
    (_oldName, newName) => navigate(projectHomePath(newName), { replace: true }),
    () => navigate("/", { replace: true }),
  );
  return (
    <div className="flex shrink-0 items-center gap-0.5">
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Project settings"
            data-testid="project-home-settings"
            className="text-muted-foreground"
            onClick={actions.openSettings}
          >
            <Settings2Icon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Project settings</TooltipContent>
      </Tooltip>
      <DropdownMenu onOpenChange={(open) => (open ? actions.onMenuOpen() : actions.onMenuClose())}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Project actions for ${projectName}`}
            data-testid="project-home-actions"
            className="text-muted-foreground"
          >
            <MoreHorizontalIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          className="min-w-40"
          onCloseAutoFocus={actions.onCloseAutoFocus}
        >
          <DropdownMenuItem onSelect={actions.openRename}>
            <PencilIcon className="size-3.5" />
            Rename project
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem variant="destructive" onSelect={actions.openDelete}>
            <Trash2Icon className="size-3.5" />
            Delete project
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {dialogs}
    </div>
  );
}

/**
 * Shows or hides the floating composer over the board. Phones open the
 * full-screen project composer instead, since it moves its pickers into the
 * header.
 */
export function ProjectNewSessionButton({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const open = useProjectComposerStore((state) => state.open);
  const setOpen = useProjectComposerStore((state) => state.setOpen);
  const label = (
    <>
      <MessageCirclePlusIcon />
      New session
    </>
  );
  if (isMobile) {
    return (
      <Button asChild data-testid="project-new-session-button">
        <Link to={`/?project=${encodeURIComponent(projectName)}`}>{label}</Link>
      </Button>
    );
  }
  return (
    <Button
      aria-expanded={open}
      data-testid="project-new-session-button"
      onClick={() => setOpen(!open)}
    >
      {label}
    </Button>
  );
}

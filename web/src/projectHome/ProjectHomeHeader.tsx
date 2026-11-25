import {
  Columns3Icon,
  MessageCirclePlusIcon,
  ScrollTextIcon,
  FileTextIcon,
  MoreHorizontalIcon,
  PencilIcon,
  SearchIcon,
  Settings2Icon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { useRef, useState } from "react";
import { create } from "zustand";
import { ProjectLandingIcon } from "@/components/ProjectIconPicker";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { MobileHeaderButton } from "@/shell/MobileHeaderButton";
import { MOBILE_GLASS_SURFACE } from "@/shell/mobileGlass";
import { SessionMenuHeader } from "@/shell/SessionMenuHeader";
import { useProjectFolderMenu } from "@/shell/Sidebar";
import { projectHomePath } from "./projectHomeNavigation";
import { useProjectTicketsEnabled, useTicketStagesEnabled } from "./useProjectTickets";
import { ProjectBoardActions } from "./ProjectBoardActions";
import { ProjectCostBadge } from "./ProjectCost";
import { ProjectDescriptionDialog } from "./ProjectDescriptionDialog";
import { useViewerId } from "@/hooks/useViewerId";
import { canEditMandate, ProjectMandateDialog, useProjectMandate } from "./ProjectMandateDialog";
import { ProjectTicketStagesDialog } from "./ProjectTicketStagesDialog";

interface ProjectComposerState {
  open: boolean;
  setOpen: (open: boolean) => void;
}

/** Whether the project home's floating composer shows; the header button and the page share it. */
export const useProjectComposerStore = create<ProjectComposerState>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));

interface ProjectBoardSearchState {
  query: string;
  setQuery: (query: string) => void;
}

/** The project home's card filter; the header's search box and the page share it. */
export const useProjectBoardSearchStore = create<ProjectBoardSearchState>((set) => ({
  query: "",
  setQuery: (query) => set({ query }),
}));

function useProjectHomeConfig(projectName: string) {
  const { data: projects, isLoading: projectsLoading, error: projectsError } = useProjects();
  const projectId = projects?.find((project) => project.name === projectName)?.id ?? null;
  const { data: config } = useProjectConfig(projectId);
  return { projectId, config, projectsLoading, projectsError };
}

/**
 * The project home's header title: its editable icon tile and name, with the
 * project actions beside it. Phones have no room for it, so they keep the name
 * for screen readers only; their actions sit in the header's right-hand group.
 */
export function ProjectHomeTitle({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const { projectId, config, projectsLoading } = useProjectHomeConfig(projectName);
  if (isMobile) return <h1 className="sr-only">{projectName}</h1>;
  return (
    <div className="flex min-w-0 items-center gap-2.5">
      <ProjectLandingIcon
        projectId={projectId}
        projectName={projectName}
        config={config}
        // The PATCH replaces the whole config, so editing waits until it loads.
        // A label-only folder (`projectId === null`) has no config to lose.
        configReady={!projectsLoading && (projectId === null || config !== undefined)}
        small
      />
      {/* The name keeps a readable minimum; the actions to its right collapse first. */}
      <h1 className="min-w-24 truncate text-lg font-medium text-foreground">{projectName}</h1>
      <div className="shrink-0 @max-[60rem]/topbar:hidden">
        <ProjectCostBadge projectId={projectId} />
      </div>
      <div className="flex shrink-0 items-center">
        <ProjectHomeActions projectName={projectName} />
      </div>
    </div>
  );
}

/** The header's right-hand actions; phones add the project actions as the session page's icon buttons. */
export function ProjectHomeHeaderActions({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const tickets = useProjectTicketsEnabled();
  return (
    <>
      {tickets ? (
        <ProjectBoardActions projectName={projectName}>
          <ProjectNewSessionButton projectName={projectName} />
        </ProjectBoardActions>
      ) : (
        <>
          <ProjectBoardSearch />
          <ProjectNewSessionButton projectName={projectName} />
        </>
      )}
      {isMobile && <ProjectHomeActions projectName={projectName} />}
    </>
  );
}

/** Project settings, plus an overflow menu to rename or delete the project. */
function ProjectHomeActions({ projectName }: { projectName: string }) {
  const stagesEnabled = useTicketStagesEnabled();
  const info = useServerInfo();
  const mandateEnabled = info !== "loading" && info.project_mandate === true;
  const descriptionEnabled = info !== "loading" && info.project_description === true;
  const [editingStages, setEditingStages] = useState(false);
  const [editingMandate, setEditingMandate] = useState(false);
  const [editingDescription, setEditingDescription] = useState(false);
  const menuTrigger = useRef<HTMLButtonElement>(null);
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const { projectId, config } = useProjectHomeConfig(projectName);
  // Members other than the owner and product approvers only read the mandate.
  const viewerId = useViewerId();
  const mandateReadOnly = !canEditMandate(
    useProjectMandate(projectId, mandateEnabled).data,
    viewerId,
  );
  const { actions, dialogs } = useProjectFolderMenu(
    projectName,
    projectId,
    config?.icon,
    (_oldName, newName) => navigate(projectHomePath(newName), { replace: true }),
    () => navigate("/", { replace: true }),
  );
  const buttonClass = cn("text-muted-foreground", isMobile && "mobile-touch-target");
  const iconClass = isMobile ? "size-5" : undefined;
  const itemClass = cn("mobile-touch-target", isMobile && "gap-2.5 px-2.5 py-2");
  return (
    <div className={isMobile ? "contents" : "flex shrink-0 items-center gap-0.5"}>
      {mandateEnabled && (
        <MandateProposalsButton
          projectId={projectId}
          className={buttonClass}
          iconClass={iconClass}
          onOpen={() => setEditingMandate(true)}
        />
      )}
      <Tooltip>
        <TooltipTrigger asChild>
          <MobileHeaderButton
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Project settings"
            data-testid="project-home-settings"
            className={buttonClass}
            onClick={actions.openSettings}
          >
            <Settings2Icon className={iconClass} />
          </MobileHeaderButton>
        </TooltipTrigger>
        <TooltipContent side="bottom">Project settings</TooltipContent>
      </Tooltip>
      {/* Non-modal on phones, as in the session header: a modal menu leaves itself the only touch target. */}
      <DropdownMenu
        modal={!isMobile}
        onOpenChange={(open) => {
          if (open) {
            actions.onMenuOpen();
          } else actions.onMenuClose();
        }}
      >
        <DropdownMenuTrigger asChild>
          <MobileHeaderButton
            ref={menuTrigger}
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label={`Project actions for ${projectName}`}
            data-testid="project-home-actions"
            className={buttonClass}
          >
            <MoreHorizontalIcon className={iconClass} />
          </MobileHeaderButton>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align={isMobile ? "end" : "start"}
          collisionPadding={isMobile ? 16 : undefined}
          className={cn(
            "min-w-40",
            MOBILE_GLASS_SURFACE,
            isMobile && "w-[calc(100vw-2rem)] max-w-[calc(100vw-2rem)]",
          )}
          onCloseAutoFocus={(event) => {
            if (editingStages || editingMandate || editingDescription) event.preventDefault();
            else actions.onCloseAutoFocus(event);
          }}
        >
          {isMobile && <SessionMenuHeader title={projectName} />}
          {stagesEnabled && (
            <DropdownMenuItem className={itemClass} onSelect={() => setEditingStages(true)}>
              <Columns3Icon className="size-3.5" />
              Edit stages
            </DropdownMenuItem>
          )}
          {mandateEnabled && (
            <DropdownMenuItem
              className={itemClass}
              data-testid="project-edit-mandate"
              onSelect={() => setEditingMandate(true)}
            >
              <ScrollTextIcon className="size-3.5" />
              {mandateReadOnly ? "View mandate" : "Edit mandate"}
            </DropdownMenuItem>
          )}
          {descriptionEnabled && (
            <DropdownMenuItem
              className={itemClass}
              data-testid="project-edit-description"
              onSelect={() => setEditingDescription(true)}
            >
              <FileTextIcon className="size-3.5" />
              Project description
            </DropdownMenuItem>
          )}
          {(stagesEnabled || mandateEnabled || descriptionEnabled) && <DropdownMenuSeparator />}
          <DropdownMenuItem className={itemClass} onSelect={actions.openRename}>
            <PencilIcon className="size-3.5" />
            Rename project
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className={itemClass}
            variant="destructive"
            onSelect={actions.openDelete}
          >
            <Trash2Icon className="size-3.5" />
            Delete project
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {dialogs}
      {editingMandate && (
        <ProjectMandateDialog
          projectName={projectName}
          onClose={() => setEditingMandate(false)}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            menuTrigger.current?.focus();
          }}
        />
      )}
      {editingDescription && (
        <ProjectDescriptionDialog
          projectName={projectName}
          onClose={() => setEditingDescription(false)}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            menuTrigger.current?.focus();
          }}
        />
      )}
      {editingStages && (
        <ProjectTicketStagesDialog
          projectName={projectName}
          onClose={() => setEditingStages(false)}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            menuTrigger.current?.focus();
          }}
        />
      )}
    </div>
  );
}

/** Appears while agents' proposed mandate additions wait for the owner. */
function MandateProposalsButton({
  projectId,
  className,
  iconClass,
  onOpen,
}: {
  projectId: string | null;
  className: string;
  iconClass?: string;
  onOpen: () => void;
}) {
  const isMobile = useIsMobileViewport();
  const pending = useProjectMandate(projectId).data?.mandate_proposals?.length ?? 0;
  if (pending === 0) return null;
  const label = `${pending} proposed mandate ${pending === 1 ? "addition" : "additions"}`;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <MobileHeaderButton
          type="button"
          variant="ghost"
          size={isMobile ? "icon-sm" : "sm"}
          aria-label={label}
          data-testid="project-mandate-proposals-button"
          className={cn(className, "text-foreground")}
          onClick={onOpen}
        >
          <ScrollTextIcon className={iconClass} />
          {!isMobile && pending}
        </MobileHeaderButton>
      </TooltipTrigger>
      <TooltipContent side="bottom">Review proposed mandate additions</TooltipContent>
    </Tooltip>
  );
}

/** Filters the board to matching cards. Desktop only; the phone header has no room. */
export function ProjectBoardSearch() {
  const isMobile = useIsMobileViewport();
  const query = useProjectBoardSearchStore((state) => state.query);
  const setQuery = useProjectBoardSearchStore((state) => state.setQuery);
  const inputRef = useRef<HTMLInputElement>(null);
  if (isMobile) return null;
  return (
    <div className="relative w-56">
      <SearchIcon
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
      />
      <Input
        ref={inputRef}
        type="search"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Filter sessions…"
        aria-label="Filter sessions"
        data-testid="project-board-search"
        className="pr-8 pl-8 [&::-webkit-search-cancel-button]:appearance-none"
      />
      {query && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Clear filter"
          className="absolute top-1/2 right-1 -translate-y-1/2 text-muted-foreground"
          onClick={() => {
            setQuery("");
            inputRef.current?.focus();
          }}
        >
          <XIcon />
        </Button>
      )}
    </div>
  );
}

function projectNewSessionPath(projectName: string) {
  return `/?project=${encodeURIComponent(projectName)}`;
}

/** Ticket boards and phones use the full project composer; desktop session boards keep it floating. */
export function ProjectNewSessionButton({
  projectName,
  compact = false,
}: {
  projectName: string;
  compact?: boolean;
}) {
  const isMobile = useIsMobileViewport();
  const tickets = useProjectTicketsEnabled();
  const navigate = useNavigate();
  const newSessionPath = projectNewSessionPath(projectName);
  const open = useProjectComposerStore((state) => state.open);
  const setOpen = useProjectComposerStore((state) => state.setOpen);
  if (isMobile || compact) {
    // A button, not a link: the phone header's button group hides itself when it holds no button.
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <MobileHeaderButton
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="New session"
            data-testid="project-new-session-button"
            className={cn("text-muted-foreground", isMobile && "mobile-touch-target")}
            onClick={() => navigate(newSessionPath)}
          >
            <MessageCirclePlusIcon className={isMobile ? "size-5" : undefined} />
          </MobileHeaderButton>
        </TooltipTrigger>
        <TooltipContent side="bottom">New session</TooltipContent>
      </Tooltip>
    );
  }
  return (
    <Button
      aria-expanded={tickets ? undefined : open}
      data-testid="project-new-session-button"
      onClick={() => {
        if (tickets) navigate(newSessionPath);
        else setOpen(!open);
      }}
    >
      <MessageCirclePlusIcon />
      New session
    </Button>
  );
}

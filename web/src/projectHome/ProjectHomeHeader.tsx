import {
  BellIcon,
  BellOffIcon,
  MessageCirclePlusIcon,
  MoreHorizontalIcon,
  PencilIcon,
  SearchIcon,
  Settings2Icon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { useCallback, useRef } from "react";
import { create } from "zustand";
import { ProjectLandingIcon } from "@/components/ProjectIconPicker";
import { useProjectHomePaletteCommands } from "@/projectHome/useProjectHomePaletteCommands";
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
import { useNavigate } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { MobileHeaderButton } from "@/shell/MobileHeaderButton";
import { MOBILE_GLASS_SURFACE } from "@/shell/mobileGlass";
import { SessionMenuHeader } from "@/shell/SessionMenuHeader";
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
  const { data: projects, isLoading: projectsLoading } = useProjects();
  const projectId = projects?.find((project) => project.name === projectName)?.id ?? null;
  const { data: config } = useProjectConfig(projectId);
  return { projectId, config, projectsLoading };
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
      <ProjectHomeActions projectName={projectName} />
    </div>
  );
}

/** The header's right-hand actions; phones add the project actions as the session page's icon buttons. */
export function ProjectHomeHeaderActions({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  return (
    <>
      <ProjectBoardSearch />
      <ProjectNewSessionButton projectName={projectName} />
      {isMobile && <ProjectHomeActions projectName={projectName} />}
    </>
  );
}

/** Project settings, plus an overflow menu to rename or delete the project. */
function ProjectHomeActions({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const { projectId, config } = useProjectHomeConfig(projectName);
  const { actions, dialogs } = useProjectFolderMenu(
    projectName,
    projectId,
    config?.icon,
    (_oldName, newName) => navigate(projectHomePath(newName), { replace: true }),
    () => navigate("/", { replace: true }),
  );
  const setComposerOpen = useProjectComposerStore((state) => state.setOpen);
  useProjectHomePaletteCommands(
    projectName,
    actions,
    // As the New session button: the floating composer, or the full-screen one on phones.
    useCallback(() => {
      if (isMobile) void navigate(`/?project=${encodeURIComponent(projectName)}`);
      else setComposerOpen(true);
    }, [isMobile, navigate, projectName, setComposerOpen]),
  );
  const buttonClass = cn("text-muted-foreground", isMobile && "mobile-touch-target");
  const iconClass = isMobile ? "size-5" : undefined;
  const itemClass = cn("mobile-touch-target", isMobile && "gap-2.5 px-2.5 py-2");
  return (
    <div className={isMobile ? "contents" : "flex shrink-0 items-center gap-0.5"}>
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
        onOpenChange={(open) => (open ? actions.onMenuOpen() : actions.onMenuClose())}
      >
        <DropdownMenuTrigger asChild>
          <MobileHeaderButton
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
          onCloseAutoFocus={actions.onCloseAutoFocus}
        >
          {isMobile && <SessionMenuHeader title={projectName} />}
          <DropdownMenuItem className={itemClass} onSelect={actions.openRename}>
            <PencilIcon className="size-3.5" />
            Rename project
          </DropdownMenuItem>
          {actions.mute && (
            <DropdownMenuItem
              className={itemClass}
              data-testid="mute-project"
              disabled={actions.mute.disabled}
              onSelect={actions.mute.toggle}
            >
              {actions.mute.muted ? (
                <BellIcon className="size-3.5" />
              ) : (
                <BellOffIcon className="size-3.5" />
              )}
              {actions.mute.muted ? "Unmute project" : "Mute project"}
            </DropdownMenuItem>
          )}
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
    </div>
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

/**
 * Shows or hides the floating composer over the board. Phones open the
 * full-screen project composer instead, since it moves its pickers into the
 * header.
 */
export function ProjectNewSessionButton({ projectName }: { projectName: string }) {
  const isMobile = useIsMobileViewport();
  const navigate = useNavigate();
  const open = useProjectComposerStore((state) => state.open);
  const setOpen = useProjectComposerStore((state) => state.setOpen);
  if (isMobile) {
    // A button, not a link: the phone header's button group hides itself when it holds no button.
    return (
      <MobileHeaderButton
        type="button"
        aria-label="New session"
        data-testid="project-new-session-button"
        className="mobile-touch-target text-muted-foreground"
        onClick={() => navigate(`/?project=${encodeURIComponent(projectName)}`)}
      >
        <MessageCirclePlusIcon className="size-5" />
      </MobileHeaderButton>
    );
  }
  return (
    <Button
      aria-expanded={open}
      data-testid="project-new-session-button"
      onClick={() => setOpen(!open)}
    >
      <MessageCirclePlusIcon />
      New session
    </Button>
  );
}

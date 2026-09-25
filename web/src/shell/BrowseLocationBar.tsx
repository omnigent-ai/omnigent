import { ArrowLeftIcon, FolderDotIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import type { WorkspaceReach } from "@/hooks/useWorkspaceChangedFiles";
import { isNavigablePath, WorkspacePicker } from "./WorkspacePicker";

interface BrowseLocationBarProps {
  /** Absolute path currently shown. */
  current: string;
  /** Absolute workspace root, so the picker can offer a one-click return. */
  workspace: string;
  /** Host whose filesystem the picker browses, or null when not host-bound. */
  hostId: string | null;
  /**
   * Whether THIS viewer may browse outside the workspace. Distinct from
   * {@link reach}, which describes what the *environment* can reach and is
   * identical for every viewer of a session — a collaborator who is not the
   * owner is refused (403) however wide the environment's own reach is.
   */
  canBrowseOutside: boolean;
  /** The session's reported reach, or null while the metadata loads. */
  reach: WorkspaceReach | null;
  /** Navigate to an absolute path. */
  onNavigate: (absolutePath: string) => void;
  /** Message shown under the path when the last navigation was refused. */
  error?: string | null;
}

/**
 * Working-folder path in the files header, clickable to browse elsewhere.
 *
 * The path opens the same directory browser the new-session flow uses to pick
 * a workspace, so choosing where to look is one interaction the user has
 * already learned — and it brings that browser's typed path, Up / Home, and
 * show-hidden along with it. Navigation applies live as the user browses (no
 * separate confirm), matching the new-session chip.
 *
 * Falls back to a plain path label when the full browser is unavailable. The
 * parent button remains enabled only while moving upward stays inside the
 * workspace, avoiding an owner-scoped host browse that would be refused.
 *
 * @param current Absolute path currently shown.
 * @param workspace Absolute workspace root, for the picker's return button.
 * @param hostId Host whose filesystem to browse.
 * @param canBrowseOutside Whether this viewer is allowed to leave the workspace.
 * @param reach The session's reported reach, or null while loading.
 * @param onNavigate Fired with the absolute path to browse to.
 * @param error Message to show when the last navigation was refused.
 */
export function BrowseLocationBar({
  current,
  workspace,
  hostId,
  canBrowseOutside,
  reach,
  onNavigate,
  error,
}: BrowseLocationBarProps) {
  const [open, setOpen] = useState(false);
  const canRoam = canBrowseOutside && (reach?.unconfined ?? false) && hostId !== null;
  const parent = parentPath(current);
  const navigableParent =
    parent !== null && (canRoam || pathIsWithin(parent, workspace)) ? parent : null;

  if (!canRoam) {
    return (
      <TooltipProvider>
        <span className="flex min-w-0 flex-1 items-center gap-[2px]">
          <ParentFolderButton parent={navigableParent} onNavigate={onNavigate} />
          <WorkspaceRootButton current={current} workspace={workspace} onNavigate={onNavigate} />
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="inline-block min-w-0 flex-1 truncate font-medium text-ui">
                {basename(current)}
              </span>
            </TooltipTrigger>
            <TooltipContent side="bottom">{current}</TooltipContent>
          </Tooltip>
        </span>
      </TooltipProvider>
    );
  }

  return (
    <span className="flex min-w-0 flex-1 flex-col">
      <span className="flex min-w-0 items-center gap-[2px]">
        <ParentFolderButton parent={navigableParent} onNavigate={onNavigate} />
        <WorkspaceRootButton current={current} workspace={workspace} onNavigate={onNavigate} />
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              title={current}
              aria-label={`Working folder: ${current}. Click to browse.`}
              className="min-w-0 flex-1 cursor-pointer rounded px-1 py-0.5 text-left hover:bg-muted hover:text-foreground"
              data-testid="browse-location-path"
            >
              <PathText path={current} />
            </button>
          </PopoverTrigger>
          {/* Cap to the viewport so the browser can't overflow a narrow panel. */}
          <PopoverContent align="start" className="w-[min(420px,calc(100vw-2rem))] p-0">
            <WorkspacePicker
              hostId={hostId}
              initialPath={isNavigablePath(current) ? current : undefined}
              workspacePath={workspace}
              onNavigate={onNavigate}
            />
          </PopoverContent>
        </Popover>
      </span>
      {error && (
        <span className="truncate text-[10px] text-destructive" data-testid="browse-location-error">
          {error}
        </span>
      )}
    </span>
  );
}

function WorkspaceRootButton({
  current,
  workspace,
  onNavigate,
}: {
  current: string;
  workspace: string;
  onNavigate: (absolutePath: string) => void;
}) {
  if (current === workspace) return null;

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Back to working folder"
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => onNavigate(workspace)}
          >
            <FolderDotIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Back to working folder</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function ParentFolderButton({
  parent,
  onNavigate,
}: {
  parent: string | null;
  onNavigate: (absolutePath: string) => void;
}) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            aria-label="Go to parent folder"
            disabled={parent === null}
            className="shrink-0 text-muted-foreground hover:text-foreground"
            onClick={() => parent && onNavigate(parent)}
          >
            <ArrowLeftIcon />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">Back one folder</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * Render an absolute path, truncating the *start* when it doesn't fit.
 *
 * A path's tail is the informative part — which folder you are in — so the
 * usual end-ellipsis would hide exactly what the user needs to read. The RTL
 * container moves the ellipsis to the front; the inner ``bdi`` keeps the path
 * itself reading left-to-right so the leading slash stays where it belongs.
 *
 * @param path Absolute path to display.
 */
function PathText({ path }: { path: string }) {
  return (
    <span dir="rtl" className="block truncate text-left font-medium text-ui">
      <bdi dir="ltr">{path}</bdi>
    </span>
  );
}

/**
 * Last path segment, or "/" for the filesystem root.
 *
 * Splits on both separators: a Windows host reports its workspace with
 * backslashes, and those sessions still need a readable folder name.
 */
function basename(absolutePath: string): string {
  if (absolutePath === "/" || absolutePath === "") return "/";
  return absolutePath.split(/[/\\]/).filter(Boolean).pop() ?? absolutePath;
}

function parentPath(absolutePath: string): string | null {
  const usesBackslashes = absolutePath.includes("\\");
  const normalized = absolutePath.replaceAll("\\", "/").replace(/\/+$/, "");
  if (normalized === "" || normalized === "/" || /^[A-Za-z]:$/.test(normalized)) return null;

  const separator = normalized.lastIndexOf("/");
  if (separator < 0) return null;
  let parent = separator === 0 ? "/" : normalized.slice(0, separator);
  if (/^[A-Za-z]:$/.test(parent)) parent += "/";
  return usesBackslashes ? parent.replaceAll("/", "\\") : parent;
}

function pathIsWithin(path: string, root: string): boolean {
  const normalize = (value: string) => value.replaceAll("\\", "/").replace(/\/+$/, "");
  const normalizedPath = normalize(path);
  const normalizedRoot = normalize(root);
  return normalizedPath === normalizedRoot || normalizedPath.startsWith(`${normalizedRoot}/`);
}

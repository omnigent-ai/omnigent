import { useState } from "react";

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
 * Falls back to a plain label when there is nowhere else to go OR this
 * viewer may not go there — a confined agent, a session with no host, or a
 * non-owner collaborator. Offering a control that is guaranteed to 403 is
 * worse than not offering it: the picker browses the owner-scoped host
 * filesystem, so for a collaborator it opens onto an error.
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

  if (!canRoam) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-block max-w-full truncate font-mono text-[11px] text-muted-foreground">
              {basename(current)}
            </span>
          </TooltipTrigger>
          <TooltipContent side="bottom">{current}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <span className="flex min-w-0 flex-1 flex-col">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <button
            type="button"
            title={current}
            aria-label={`Working folder: ${current}. Click to browse.`}
            className="min-w-0 rounded px-1 py-0.5 text-left hover:bg-accent hover:text-accent-foreground"
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
      {error && (
        <span className="truncate text-[10px] text-destructive" data-testid="browse-location-error">
          {error}
        </span>
      )}
    </span>
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
    <span
      dir="rtl"
      className="block truncate text-left font-mono text-[11px] text-muted-foreground"
    >
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

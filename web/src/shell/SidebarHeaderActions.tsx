import { PanelLeftOpenIcon, PanelRightOpenIcon, SearchIcon, SettingsIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";

/**
 * Search / Settings / sidebar-toggle cluster from the sidebar's header row.
 *
 * Extracted so it can render in TWO places without duplicating the markup:
 * inside the sidebar (every browser and non-mac platform) and, on the macOS
 * desktop shell, in the persistent title-bar strip. There it must survive the
 * sidebar collapsing — the sidebar goes `md:w-0 md:overflow-hidden` and turns
 * `inert`, so a cluster living inside it would be clipped and unclickable, and
 * while peeking the card floats at `inset-2` and drags the cluster off the
 * traffic lights' centre line. Hoisting it out of the sidebar is what keeps the
 * three icons in one fixed place across open, collapsed, and peeking.
 *
 * Only the toggle's meaning changes with state: it collapses an open sidebar and
 * opens a closed or peeking one, so the caller passes `expanded` and the icon
 * and label follow from it.
 */
export function SidebarHeaderActions({
  expanded,
  onToggle,
  onOpenSearch,
  onSettingsClick,
  onTogglePointerEnter,
  onTogglePointerLeave,
}: {
  /**
   * Whether the sidebar currently reads as open — `open || peek`. Drives the
   * toggle only: expanded collapses, collapsed (or peeking) opens.
   */
  expanded: boolean;
  /** Collapse an expanded sidebar, or open a collapsed/peeking one. */
  onToggle: () => void;
  /** Open the command palette (search). Optional so the cluster can render standalone. */
  onOpenSearch?: () => void;
  /**
   * Extra handler for the Settings link. The in-sidebar copy passes nothing (on
   * mobile, entering Settings keeps the drawer open and swaps it to the section
   * list); navigation itself is the Link's job.
   */
  onSettingsClick?: () => void;
  /**
   * Dwell-to-peek hooks for the toggle. On the macOS shell this cluster replaces
   * ChatHeader's collapsed-state button, which is where peek used to be armed,
   * so the behaviour has to ride along with it or dwell-to-peek disappears.
   */
  onTogglePointerEnter?: () => void;
  onTogglePointerLeave?: () => void;
}) {
  return (
    <div className="flex items-center gap-1" data-testid="sidebar-header-actions">
      <SidebarSearchButton onOpenSearch={onOpenSearch} />
      <SidebarSettingsButton onSettingsClick={onSettingsClick} className="max-md:hidden" />
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={expanded ? "Close sidebar" : "Open sidebar"}
            onClick={onToggle}
            onPointerEnter={onTogglePointerEnter}
            onPointerLeave={onTogglePointerLeave}
            // Mobile drops the toggle entirely: there the sidebar is a drawer
            // that leaves a strip of the chat visible, and tapping that strip
            // is how you get back — a collapse icon would be a second, worse
            // way to do the same thing.
            className="size-6 text-muted-foreground hover:text-foreground max-md:hidden"
          >
            {/* panel-right-open reads as "push the panel away" while the sidebar
            is open; panel-left-open as "bring it back" once it is collapsed or
            merely peeking. */}
            {expanded ? (
              <PanelRightOpenIcon className="ui-icon" />
            ) : (
              <PanelLeftOpenIcon className="ui-icon" />
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="bottom">
          {expanded ? "Collapse sidebar" : "Open sidebar"}
        </TooltipContent>
      </Tooltip>
    </div>
  );
}

/**
 * Mobile treatment shared by the two floating icon buttons: a round chip that
 * reads as lifted off the session list, sized for a thumb. On desktop these
 * stay flat 24px ghost icons in the header row.
 */
const SIDEBAR_FLOAT_BUTTON =
  "size-6 text-muted-foreground hover:text-foreground max-md:size-9 max-md:rounded-full max-md:bg-muted/70 max-md:text-foreground";

const SIDEBAR_FLOAT_ICON = "ui-icon max-md:size-[18px]";

/**
 * Search (command palette) icon button.
 *
 * Split out of the cluster because the mobile sidebar places it on its own —
 * floating at the top of the drawer, away from Settings, which floats at the
 * bottom.
 *
 * @param onOpenSearch - Opens the command palette. Optional so the button can
 *   render standalone (e.g. in tests).
 * @param className - Extra classes, e.g. the mobile float's positioning.
 */
export function SidebarSearchButton({
  onOpenSearch,
  className,
}: {
  onOpenSearch?: () => void;
  className?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label="Search"
          onClick={() => onOpenSearch?.()}
          className={cn(SIDEBAR_FLOAT_BUTTON, className)}
          data-testid="sidebar-search-button"
        >
          <SearchIcon className={SIDEBAR_FLOAT_ICON} />
        </Button>
      </TooltipTrigger>
      {/* Bottom placement keeps the tooltip clear of the macOS Electron
      shell's traffic lights at the window's top edge. */}
      <TooltipContent side="bottom">Search</TooltipContent>
    </Tooltip>
  );
}

/**
 * Settings icon button — a `Link` to `/settings`.
 *
 * @param onSettingsClick - Extra click handler; navigation itself is the
 *   Link's job.
 * @param className - Extra classes, e.g. the mobile float's positioning.
 * @param testId - `data-testid` on the link.
 */
export function SidebarSettingsButton({
  onSettingsClick,
  className,
  testId = "settings-button",
}: {
  onSettingsClick?: () => void;
  className?: string;
  /** Distinguishes the header-row copy from the mobile floating copy. */
  testId?: string;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          asChild
          variant="ghost"
          size="icon-xs"
          aria-label="Settings"
          className={cn(SIDEBAR_FLOAT_BUTTON, className)}
        >
          <Link to="/settings" onClick={onSettingsClick} data-testid={testId}>
            <SettingsIcon className={SIDEBAR_FLOAT_ICON} />
          </Link>
        </Button>
      </TooltipTrigger>
      <TooltipContent side="bottom">Settings</TooltipContent>
    </Tooltip>
  );
}

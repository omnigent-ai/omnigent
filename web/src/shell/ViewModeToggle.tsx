import { CheckIcon, Loader2Icon, MessagesSquareIcon, TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenuItem, DropdownMenuSeparator } from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";
import { useTerminalFirst } from "./TerminalFirstContext";

/**
 * Header Chat/Terminal switcher for terminal-first sessions. Two icon
 * segments in a shared track show both destinations at once, so the
 * active view is readable at a glance and switching is one click — no
 * menu to open. Status lives in the sidebar; this is purely a view
 * toggle.
 *
 * Self-gates to null when there's nothing to toggle:
 *   - non-terminal-first sessions,
 *   - a rail-opened shell owning the main view (isShellView) — its own
 *     close affordance is the way back to chat.
 *
 * On a desktop-width viewport it renders in the header on every shell — the
 * native iOS bottom pill is retired (AppShell pushes it hidden at boot), so the
 * header is the one placement. On a mobile-width viewport the segmented track
 * is dropped and the switch folds into the header kebab instead (see
 * {@link ViewModeMenuItems}), so the narrow header pill isn't split between a
 * view switch and the "…" menu.
 */
export function ViewModeToggle() {
  const ctx = useTerminalFirst();
  const isMobile = useIsMobileViewport();
  if (!ctx || !ctx.isTerminalFirst || ctx.isShellView) return null;
  // Mobile folds the switch into the header kebab (ViewModeMenuItems).
  if (isMobile) return null;

  const { view, setView, terminalStartingUp } = ctx;
  const terminalLabel = terminalStartingUp ? "Terminal is starting up…" : "Terminal view";

  return (
    <div
      role="group"
      aria-label="Switch between chat and terminal"
      data-testid="view-mode-toggle"
      // Lets the mobile header pill inset itself only when this segmented
      // track is present — see MOBILE_GLASS_PILL.
      data-slot="view-mode-toggle"
      // Inset track: p-0.5 around two size-6 segments lands the control at
      // 32px tall, matching the header's other controls. On mobile the
      // segments grow to the 44px tap-target floor and the padding drops so
      // the track matches the pill's other 44px controls instead of
      // stretching it — and the flush track must be a capsule like those
      // segments and the glass pill, or its grey paints squared corners
      // between two round shapes.
      className="flex items-center gap-0.5 rounded-[var(--radius-lg)] bg-muted/60 p-0.5 max-md:rounded-full max-md:p-0"
    >
      <ViewModeSegment
        label="Chat view"
        active={view === "chat"}
        onClick={() => setView("chat")}
        testId="view-mode-chat"
        componentId="chat.header.view_chat"
      >
        <MessagesSquareIcon className="size-3.5 max-md:size-5" />
      </ViewModeSegment>
      <ViewModeSegment
        label={terminalLabel}
        active={view === "terminal"}
        onClick={() => setView("terminal")}
        testId="view-mode-terminal"
        componentId="chat.header.view_terminal"
      >
        {terminalStartingUp ? (
          <Loader2Icon className="size-3.5 animate-spin max-md:size-5" aria-hidden />
        ) : (
          <TerminalIcon className="size-3.5 max-md:size-5" />
        )}
      </ViewModeSegment>
    </div>
  );
}

/**
 * One segment of the switcher: an icon-only button whose name lives in a
 * tooltip. `aria-pressed` (not a radio) so each segment reads as an
 * independent toggle to AT, matching how it behaves — clicking the active
 * segment is a no-op rather than a selection change.
 */
function ViewModeSegment({
  label,
  active,
  onClick,
  testId,
  componentId,
  children,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  testId: string;
  componentId: string;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex">
          <Button
            type="button"
            variant={active ? "secondary" : "ghost"}
            size="icon-xs"
            aria-label={label}
            aria-pressed={active}
            onClick={onClick}
            data-testid={testId}
            componentId={componentId}
            className={cn(
              // 44px tap-target floor on phones, matching the sibling header
              // controls; icon-xs alone is a 24px hit box a finger misses.
              "border-none max-md:size-11 max-md:rounded-full",
              active
                ? "bg-background text-foreground shadow-sm hover:bg-background"
                : "text-muted-foreground hover:bg-transparent hover:text-foreground",
            )}
          >
            {children}
          </Button>
        </span>
      </TooltipTrigger>
      {/* Bottom placement: the header sits at top-0, so a top-side tooltip
          would render above the viewport edge and get clipped. */}
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

/**
 * Chat/Terminal switch as dropdown-menu items — the mobile counterpart of the
 * {@link ViewModeToggle} segmented track, folded into the header kebab so the
 * narrow header pill carries one trigger instead of a switch beside the "…"
 * menu. Self-gates to null on the same conditions as the toggle (non
 * terminal-first sessions, and a shell owning the main view). The active view
 * carries a trailing check; a trailing separator sets the switch off from the
 * menu items that follow.
 */
export function ViewModeMenuItems() {
  const ctx = useTerminalFirst();
  if (!ctx || !ctx.isTerminalFirst || ctx.isShellView) return null;

  const { view, setView, terminalStartingUp } = ctx;
  const terminalLabel = terminalStartingUp ? "Terminal (starting up…)" : "Terminal";

  return (
    <>
      <DropdownMenuItem
        className="gap-2.5 px-2.5 py-2 text-ui"
        onSelect={() => setView("chat")}
        data-testid="view-mode-menu-chat"
      >
        <MessagesSquareIcon className="size-4" />
        Chat
        {view === "chat" && <CheckIcon className="ml-auto size-4" />}
      </DropdownMenuItem>
      <DropdownMenuItem
        className="gap-2.5 px-2.5 py-2 text-ui"
        onSelect={() => setView("terminal")}
        data-testid="view-mode-menu-terminal"
      >
        {terminalStartingUp ? (
          <Loader2Icon className="size-4 animate-spin" aria-hidden />
        ) : (
          <TerminalIcon className="size-4" />
        )}
        {terminalLabel}
        {view === "terminal" && <CheckIcon className="ml-auto size-4" />}
      </DropdownMenuItem>
      <DropdownMenuSeparator />
    </>
  );
}

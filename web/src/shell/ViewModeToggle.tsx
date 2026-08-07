import { useRef, useState } from "react";
import { ChevronDownIcon, Loader2Icon, MessagesSquareIcon, TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { isIOSShell } from "@/lib/nativeBridge";
import { type TerminalFirstView, useTerminalFirst } from "./TerminalFirstContext";

/**
 * Header Chat/Terminal switcher for terminal-first sessions. A
 * MessagesSquare + chevron trigger opens a small menu with Chat and
 * Terminal options (the current view is checked), replacing the old
 * in-page pill above the composer. Status lives in the sidebar — this is
 * purely a view toggle.
 *
 * Self-gates to null when there's nothing to toggle:
 *   - non-terminal-first sessions,
 *   - the iOS shell (the switcher is the native Liquid Glass bar there),
 *   - a rail-opened shell owning the main view (isShellView) — its own
 *     close affordance is the way back to chat.
 */
export function ViewModeToggle() {
  const ctx = useTerminalFirst();
  const [open, setOpen] = useState(false);
  // Drive the tooltip from the trigger's own hover/focus rather than a nested
  // TooltipTrigger: stacking TooltipTrigger + DropdownMenuTrigger asChild onto
  // one Button merges two Slots and the tooltip's listeners can get dropped.
  // Suppress it while the menu is open so it never overlaps the dropdown.
  const [hovered, setHovered] = useState(false);
  // Tracks how the last menu interaction ended so close-focus handling can tell
  // a pointer close (suppress refocus — a restored ghost-button focus ring reads
  // as a stuck highlight) from a keyboard close (restore focus so keyboard/AT
  // users keep their place).
  const closedByPointerRef = useRef(false);
  if (!ctx || !ctx.isTerminalFirst || ctx.isShellView || isIOSShell()) return null;

  const { view, setView, terminalsAvailable, terminalStartingUp } = ctx;

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      {/* Tooltip anchors to a wrapper span (a single clean Slot) rather than
          stacking TooltipTrigger + DropdownMenuTrigger onto the same Button —
          two merged Slots on one node drop the tooltip's hover listeners. The
          span carries the hover/focus that drives the controlled tooltip; the
          DropdownMenuTrigger keeps anchoring the menu to the Button. Suppressed
          while the menu is open so it never overlaps the dropdown. */}
      <Tooltip open={hovered && !open}>
        <TooltipTrigger asChild>
          <span
            className="inline-flex"
            onPointerEnter={() => setHovered(true)}
            onPointerLeave={() => setHovered(false)}
            onFocus={() => setHovered(true)}
            onBlur={() => setHovered(false)}
          >
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                aria-label="Switch between chat and terminal"
                data-testid="view-mode-toggle"
                className="w-11 gap-1 px-0 text-muted-foreground hover:text-foreground border-none"
              >
                <MessagesSquareIcon className="size-4" />
                <ChevronDownIcon className="size-3 opacity-60" />
              </Button>
            </DropdownMenuTrigger>
          </span>
        </TooltipTrigger>
        {/* Bottom placement: the header sits at top-0, so a top-side tooltip
            would render above the viewport edge and get clipped. */}
        <TooltipContent side="bottom">
          {view === "terminal" ? "Terminal view" : "Chat view"}
        </TooltipContent>
      </Tooltip>
      <DropdownMenuContent
        align="end"
        className="min-w-40"
        // On a pointer close, don't snap focus back to the trigger: a ghost
        // button keeps its focus ring lit until the next click, reading as a
        // stuck "selected" state. On a keyboard close, let focus return so
        // keyboard/AT users keep their place.
        onCloseAutoFocus={(e) => {
          if (closedByPointerRef.current) e.preventDefault();
          closedByPointerRef.current = false;
        }}
        onPointerDownCapture={() => {
          closedByPointerRef.current = true;
        }}
      >
        <DropdownMenuRadioGroup
          value={view}
          onValueChange={(next) => setView(next as TerminalFirstView)}
        >
          <DropdownMenuRadioItem value="chat" className="gap-2">
            <MessagesSquareIcon className="size-4" />
            Chat
          </DropdownMenuRadioItem>
          {/* Terminal disabled until a PTY is reachable; a spinner while it's
              coming up reads as "loading" rather than a dead option. */}
          <DropdownMenuRadioItem
            value="terminal"
            className="gap-2"
            disabled={!terminalsAvailable}
            title={terminalStartingUp ? "Terminal is starting up…" : undefined}
          >
            {terminalStartingUp ? (
              <Loader2Icon className="size-4 animate-spin" aria-hidden />
            ) : (
              <TerminalIcon className="size-4" />
            )}
            Terminal
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

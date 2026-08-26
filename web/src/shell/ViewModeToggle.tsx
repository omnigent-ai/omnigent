import { Loader2Icon, MessagesSquareIcon, TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { isIOSShell } from "@/lib/nativeBridge";
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
 *   - the iOS shell (the switcher is the native Liquid Glass bar there),
 *   - a rail-opened shell owning the main view (isShellView) — its own
 *     close affordance is the way back to chat.
 */
export function ViewModeToggle() {
  const ctx = useTerminalFirst();
  if (!ctx || !ctx.isTerminalFirst || ctx.isShellView || isIOSShell()) return null;

  const { view, setView, terminalStartingUp } = ctx;
  const terminalLabel = terminalStartingUp ? "Terminal is starting up…" : "Terminal view";

  return (
    <div
      role="group"
      aria-label="Switch between chat and terminal"
      data-testid="view-mode-toggle"
      // Inset track: p-0.5 around two size-6 segments lands the control at
      // 32px tall, matching the header's other controls.
      className="flex items-center gap-0.5 rounded-[var(--radius-lg)] bg-muted/60 p-0.5"
    >
      <ViewModeSegment
        label="Chat view"
        active={view === "chat"}
        onClick={() => setView("chat")}
        testId="view-mode-chat"
        componentId="chat.header.view_chat"
      >
        <MessagesSquareIcon className="size-3.5" />
      </ViewModeSegment>
      <ViewModeSegment
        label={terminalLabel}
        active={view === "terminal"}
        onClick={() => setView("terminal")}
        testId="view-mode-terminal"
        componentId="chat.header.view_terminal"
      >
        {terminalStartingUp ? (
          <Loader2Icon className="size-3.5 animate-spin" aria-hidden />
        ) : (
          <TerminalIcon className="size-3.5" />
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
              "border-none",
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

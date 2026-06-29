// "+ New shell" affordance shared by the shell surfaces
// (InlineTerminalsSection, TerminalsPanel, and MainTerminalView's
// strip on native-wrapper sessions).
//
// Gated on the agent's terminal access: the button renders ONLY when
// the session agent's spec declares a non-empty `terminals:` block
// (read via `useSessionAgent().terminals` — the same gate the server
// enforces on POST /resources/terminals and that controls the agent's
// own sys_terminal_* tool surface). An agent without terminal access
// never sees the button, and a user-created terminal is always one
// the agent can list/read/close.
//
// One declared name creates directly on click; multiple names open a
// dropdown to pick which declared terminal to launch.

import { PlusIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useSessionAgent } from "@/hooks/useAgents";
import { terminalTabKey, useCreateTerminal } from "@/hooks/useTerminals";

interface NewTerminalButtonProps {
  conversationId: string;
  /**
   * Called with the created terminal's tab key (e.g.
   * `"terminal:terminal_shell_u-abc123"`) so the host surface can
   * focus the new terminal. Optional — surfaces without an active-key
   * concept omit it.
   */
  onCreated?: (terminalKey: string) => void;
  /**
   * Visual shape. `"icon"` (default) is the compact tooltip-wrapped
   * plus used in headers and tab strips; `"row"` renders as a
   * full-width list row (plus icon + "New shell" label) so the
   * affordance sits at the end of the Shells list like a virtual
   * entry rather than floating in empty space.
   */
  variant?: "icon" | "row";
}

export function NewTerminalButton({
  conversationId,
  onCreated,
  variant = "icon",
}: NewTerminalButtonProps) {
  const { data: agent } = useSessionAgent(conversationId);
  const create = useCreateTerminal(conversationId);
  const declared = agent?.terminals ?? [];
  // The iff gate, UI side: no declared terminals → no affordance.
  if (declared.length === 0) return null;

  const launch = (name: string) => {
    create.mutate(name, {
      onSuccess: (info) => onCreated?.(terminalTabKey(info)),
    });
  };

  // Single declared name: create directly on click. Multiple: the
  // DropdownMenuTrigger wrapper below owns the click instead.
  const onTriggerClick = declared.length === 1 ? () => launch(declared[0]) : undefined;
  const trigger =
    variant === "row" ? (
      <button
        type="button"
        aria-label="New shell"
        disabled={create.isPending}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-muted-foreground hover:bg-accent/60 hover:text-foreground disabled:cursor-default disabled:opacity-50"
        onClick={onTriggerClick}
      >
        <PlusIcon className="size-3.5 shrink-0" />
        <span className="text-xs">
          {create.isError ? `Failed: ${create.error.message}` : "New shell"}
        </span>
      </button>
    ) : (
      <button
        type="button"
        aria-label="New shell"
        disabled={create.isPending}
        className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-default disabled:opacity-50"
        onClick={onTriggerClick}
      >
        <PlusIcon className="size-3.5" />
      </button>
    );

  // The row variant carries its own label (and inline error text), so
  // the tooltip is icon-only chrome.
  const withTooltip = (child: React.ReactNode) =>
    variant === "row" ? (
      child
    ) : (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{child}</TooltipTrigger>
          <TooltipContent side="bottom">
            {create.isError ? `Failed: ${create.error.message}` : "New shell"}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );

  if (declared.length === 1) return withTooltip(trigger);

  return (
    <DropdownMenu>
      {withTooltip(<DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>)}
      <DropdownMenuContent align={variant === "row" ? "start" : "end"}>
        {declared.map((name) => (
          <DropdownMenuItem key={name} onSelect={() => launch(name)}>
            {name}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

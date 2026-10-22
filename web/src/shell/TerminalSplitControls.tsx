// Header controls that split the main terminal view's focused pane into a
// new shell (right or below) and close the focused shell pane. Gated on
// `splittable_terminal_view_ui`, a desktop viewport, the agent's Terminal
// view being shown, owner access, and the agent declaring shell types.

import { useState, type ReactNode } from "react";
import { Columns2Icon, Rows2Icon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { showToast } from "@/components/ui/toast";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useSessionAgent } from "@/hooks/useAgents";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import {
  findAgentTerminal,
  terminalTabKey,
  useCreateTerminal,
  useDeleteTerminal,
  useTerminals,
} from "@/hooks/useTerminals";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isOwnerLevel } from "@/lib/permissionsApi";
import {
  splitTerminalLayout,
  terminalLayoutKeys,
  type TerminalSplitAxis,
} from "@/lib/terminalSplitLayout";
import { CloseShellDialog } from "./CloseShellDialog";
import { resolveDefaultShell, writePreferredShell } from "./preferredShell";
import { useTerminalFirst } from "./TerminalFirstContext";
import { updateTerminalSplit, useTerminalSplitPanes } from "./terminalSplitPanesStore";

export function TerminalSplitControls({
  conversationId,
  permissionLevel = null,
}: {
  conversationId: string;
  permissionLevel?: number | null;
}) {
  const enabled = isFeatureEnabled(useServerInfo(), "splittable_terminal_view_ui");
  const terminalFirst = useTerminalFirst();
  const isMobile = useIsMobileViewport();
  const { data: agent } = useSessionAgent(conversationId);
  const { terminals } = useTerminals(conversationId);
  const create = useCreateTerminal(conversationId);
  const deleteTerminal = useDeleteTerminal(conversationId);
  const split = useTerminalSplitPanes(conversationId);
  const [closeOpen, setCloseOpen] = useState(false);
  const agentTerminal = findAgentTerminal(terminals);
  const declaredShells = agent?.terminals ?? [];
  if (
    !enabled ||
    isMobile ||
    !terminalFirst?.isTerminalFirst ||
    terminalFirst.isShellView ||
    terminalFirst.view !== "terminal" ||
    agentTerminal === null ||
    declaredShells.length === 0 ||
    !isOwnerLevel(permissionLevel)
  ) {
    return null;
  }

  const agentKey = terminalTabKey(agentTerminal);
  const defaultShell = resolveDefaultShell(declaredShells) as string;
  const shellOptions = [defaultShell, ...declaredShells.filter((name) => name !== defaultShell)];
  const focusedShell = split
    ? terminals.find(
        (terminal) => terminalTabKey(terminal) === split.focusedKey && terminal !== agentTerminal,
      )
    : undefined;

  const splitWith = (shell: string, axis: TerminalSplitAxis) => {
    writePreferredShell(shell);
    create.mutate(shell, {
      onSuccess: (info) => {
        const newKey = terminalTabKey(info);
        updateTerminalSplit(conversationId, (current) => {
          const layout = current?.layout ?? agentKey;
          const target = current?.focusedKey ?? agentKey;
          if (!terminalLayoutKeys(layout).includes(target)) return current;
          return { layout: splitTerminalLayout(layout, target, newKey, axis), focusedKey: newKey };
        });
      },
      onError: (error) => showToast(`Couldn't open a shell: ${error.message}`),
    });
  };

  return (
    <div className="hidden items-center md:flex" data-testid="terminal-split-controls">
      <SplitMenu
        label="Split right"
        icon={<Columns2Icon className="size-3.5" />}
        disabled={create.isPending}
        shells={shellOptions}
        onPick={(shell) => splitWith(shell, "columns")}
      />
      <SplitMenu
        label="Split down"
        icon={<Rows2Icon className="size-3.5" />}
        disabled={create.isPending}
        shells={shellOptions}
        onPick={(shell) => splitWith(shell, "rows")}
      />
      {focusedShell && (
        <HeaderTooltip label="Close pane">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Close pane"
            disabled={deleteTerminal.isPending}
            onClick={() => setCloseOpen(true)}
            className="border-none text-muted-foreground hover:text-foreground"
          >
            <XIcon className="size-3.5" />
          </Button>
        </HeaderTooltip>
      )}
      <CloseShellDialog
        open={closeOpen && focusedShell !== undefined}
        shellLabel={
          focusedShell
            ? focusedShell.session
              ? `${focusedShell.name} · ${focusedShell.session}`
              : focusedShell.name
            : null
        }
        onCancel={() => setCloseOpen(false)}
        onConfirm={() => {
          // MainTerminalView prunes the pane once the terminal leaves the list.
          if (focusedShell) deleteTerminal.mutate(focusedShell.id);
          setCloseOpen(false);
        }}
      />
    </div>
  );
}

/** One split direction: a header icon opening the shell-type picker, default first. */
function SplitMenu({
  label,
  icon,
  disabled,
  shells,
  onPick,
}: {
  label: string;
  icon: ReactNode;
  disabled: boolean;
  shells: string[];
  onPick: (shell: string) => void;
}) {
  return (
    <DropdownMenu>
      <HeaderTooltip label={label}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={label}
            disabled={disabled}
            className="border-none text-muted-foreground hover:text-foreground"
          >
            {icon}
          </Button>
        </DropdownMenuTrigger>
      </HeaderTooltip>
      {/* Don't hand focus back to the trigger: the new pane claims it on connect,
          and typing into a refocused trigger would reopen the menu. */}
      <DropdownMenuContent align="end" onCloseAutoFocus={(event) => event.preventDefault()}>
        <DropdownMenuLabel>{label} with</DropdownMenuLabel>
        {shells.map((shell, index) => (
          <DropdownMenuItem key={shell} onSelect={() => onPick(shell)}>
            {index === 0 ? `${shell} (default)` : shell}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function HeaderTooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      {/* The header sits at top-0, so a top-side tooltip would be clipped. */}
      <TooltipContent side="bottom">{label}</TooltipContent>
    </Tooltip>
  );
}

// Header split/close controls for the main terminal view (flag-gated, desktop owners).

import { useState, type ReactNode } from "react";
import { Columns2Icon, Rows2Icon, XIcon } from "lucide-react";
import { SPLIT_TERMINAL_KEYS } from "@/components/KeyboardShortcut";
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
import { useTerminalSplitHotkey } from "@/hooks/useTerminalSplitHotkey";
import {
  findAgentTerminal,
  isAgentTerminalKey,
  terminalDisplayLabel,
  terminalTabKey,
  type TerminalInfo,
  useCreateTerminal,
  useDeleteTerminal,
  useTerminals,
} from "@/hooks/useTerminals";
import { isFeatureEnabled } from "@/lib/capabilities";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { isOwnerLevel } from "@/lib/permissionsApi";
import {
  removeTerminalLayoutKey,
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
  // The shell a pending close targets, fixed when the dialog opens.
  const [closeTarget, setCloseTarget] = useState<TerminalInfo | null>(null);
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
  // A focused shell pane whose terminal isn't listed has nothing to kill: closing just drops it.
  const focusedMissingPane =
    split && !focusedShell && !isAgentTerminalKey(split.focusedKey) ? split.focusedKey : null;
  const closePane = () => {
    if (focusedShell) {
      setCloseTarget(focusedShell);
      return;
    }
    updateTerminalSplit(conversationId, (current) => {
      const layout = current && removeTerminalLayoutKey(current.layout, focusedMissingPane ?? "");
      if (!layout || typeof layout === "string") return undefined;
      return { layout, focusedKey: terminalLayoutKeys(layout)[0] };
    });
  };

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
        axis="columns"
        icon={<Columns2Icon className="size-3.5" />}
        disabled={create.isPending}
        shells={shellOptions}
        onPick={(shell) => splitWith(shell, "columns")}
      />
      <SplitMenu
        label="Split down"
        axis="rows"
        icon={<Rows2Icon className="size-3.5" />}
        disabled={create.isPending}
        shells={shellOptions}
        onPick={(shell) => splitWith(shell, "rows")}
      />
      {(focusedShell || focusedMissingPane) && (
        <HeaderTooltip label="Close pane">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label="Close pane"
            disabled={deleteTerminal.isPending}
            onClick={closePane}
            className="border-none text-muted-foreground hover:text-foreground"
          >
            <XIcon className="size-3.5" />
          </Button>
        </HeaderTooltip>
      )}
      <CloseShellDialog
        open={closeTarget !== null}
        shellLabel={closeTarget && terminalDisplayLabel(closeTarget)}
        onCancel={() => setCloseTarget(null)}
        onConfirm={() => {
          // MainTerminalView prunes the pane once the terminal leaves the list.
          if (closeTarget) deleteTerminal.mutate(closeTarget.id);
          setCloseTarget(null);
        }}
      />
    </div>
  );
}

/** One split direction: a header icon opening the shell-type picker, default first. */
function SplitMenu({
  label,
  axis,
  icon,
  disabled,
  shells,
  onPick,
}: {
  label: string;
  axis: TerminalSplitAxis;
  icon: ReactNode;
  disabled: boolean;
  shells: string[];
  onPick: (shell: string) => void;
}) {
  // The chord skips the picker and opens the default shell. It stays bound while a
  // create is pending so a second press is still claimed instead of reaching the PTY.
  useTerminalSplitHotkey(axis, () => {
    if (!disabled) onPick(shells[0]);
  });
  return (
    <DropdownMenu>
      <HeaderTooltip label={label} shortcut={SPLIT_TERMINAL_KEYS[axis]}>
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

function HeaderTooltip({
  label,
  shortcut,
  children,
}: {
  label: string;
  shortcut?: string[];
  children: ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      {/* The header sits at top-0, so a top-side tooltip would be clipped. */}
      <TooltipContent side="bottom" shortcut={shortcut}>
        {label}
      </TooltipContent>
    </Tooltip>
  );
}

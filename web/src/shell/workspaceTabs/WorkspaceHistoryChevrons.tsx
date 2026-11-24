// ‹ › at the left of the workspace tab strip: Back and Forward through the
// history of places across tabs. They render the shared commands, so their
// tooltips, reasons and chords match ⌘K.

import { ChevronLeftIcon, ChevronRightIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { shortcutAriaKeys, shortcutKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";

import type {
  WorkspaceHistoryCommand,
  WorkspaceHistoryDirection,
} from "../workspaceCommands/model";

export function WorkspaceHistoryChevrons({
  commands,
  onGo,
}: {
  commands: readonly WorkspaceHistoryCommand[];
  onGo: (direction: WorkspaceHistoryDirection) => void;
}) {
  if (commands.length === 0) return null;
  return (
    <div className="flex shrink-0 items-center" role="group" aria-label="Workspace history">
      {commands.map((command) => (
        <HistoryChevron key={command.id} command={command} onGo={onGo} />
      ))}
    </div>
  );
}

function HistoryChevron({
  command,
  onGo,
}: {
  command: WorkspaceHistoryCommand;
  onGo: (direction: WorkspaceHistoryDirection) => void;
}) {
  const availability = command.availability;
  const back = command.direction === "back";
  const Icon = back ? ChevronLeftIcon : ChevronRightIcon;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* aria-disabled, not disabled, so the tooltip can still say why. */}
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={back ? "Back" : "Forward"}
          aria-disabled={!availability.available || undefined}
          aria-keyshortcuts={command.shortcut ? shortcutAriaKeys(command.shortcut) : undefined}
          data-testid={`workspace-history-${command.direction}`}
          onClick={() => {
            if (availability.available) onGo(command.direction);
          }}
          className="size-6 text-muted-foreground hover:text-foreground aria-disabled:cursor-default aria-disabled:opacity-50 aria-disabled:hover:bg-transparent aria-disabled:hover:text-muted-foreground"
        >
          <Icon className="size-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent
        side="bottom"
        shortcut={
          availability.available && command.shortcut ? shortcutKeys(command.shortcut) : undefined
        }
      >
        <span>{availability.available ? command.menuLabel : availability.reason}</span>
        {command.note && (
          <span className="block max-w-64 text-muted-foreground">{command.note}</span>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

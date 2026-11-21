// One file action as a menu row, drawn from the shared model: hidden when it
// doesn't apply, and disabled with its reason when it can't run here.

import type { ComponentProps } from "react";
import { SessionActionMenuItem } from "@/components/SessionActionMenuItem";
import { fileCommandMenuIcon } from "./icons";
import type { FileCommand, FileCommandId } from "./model";

export function FileCommandMenuItem({
  command,
  onRun,
  Item,
}: {
  command: FileCommand;
  onRun: (id: FileCommandId) => void;
  Item?: ComponentProps<typeof SessionActionMenuItem>["Item"];
}) {
  const { availability } = command;
  if (availability.available === false && availability.reason === undefined) return null;
  return (
    <SessionActionMenuItem
      Item={Item}
      data-testid={`file-command-${command.id}`}
      disabledReason={availability.available ? undefined : availability.reason}
      onSelect={() => onRun(command.id)}
    >
      {fileCommandMenuIcon(command.id)}
      {command.label}
    </SessionActionMenuItem>
  );
}

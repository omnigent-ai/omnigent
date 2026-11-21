// A surface's file actions for one path: the model's commands for its target,
// and a runner that asks the action host to carry one out.

import { useCallback, useMemo } from "react";
import { fileCommands, type FileCommand, type FileCommandId } from "./model";
import { requestFileAction } from "./fileActionStore";
import {
  type FileCommandTarget,
  type FileCommandTargetOptions,
  useFileCommandTarget,
} from "./target";
import { useFileCommandFacts } from "./useFileCommandFacts";

export interface FileCommandsForPath {
  target: FileCommandTarget | null;
  commands: readonly FileCommand[];
  run: (id: FileCommandId) => void;
}

const NO_COMMANDS: readonly FileCommand[] = [];

export interface FileCommandOptions {
  /** Opens the file where this surface shows files; without it, Open isn't offered. */
  onOpen?: (() => void) | null;
  /** Opens the file in a workspace tab of its own; without it, Open in new tab isn't offered. */
  onOpenInNewTab?: (() => void) | null;
  /** Makes the folder the surface's root; without it, Open folder as root isn't offered. */
  onOpenAsRoot?: (() => void) | null;
  /** The query parameters a copied link keeps, read when it's copied, such as the viewer's diff state. */
  linkState?: () => string;
}

/** The actions for `path` as this surface names it. */
export function useFileCommands(
  path: string | null,
  {
    onOpen,
    onOpenInNewTab,
    onOpenAsRoot,
    linkState,
    ...targetOptions
  }: FileCommandOptions & FileCommandTargetOptions = {},
): FileCommandsForPath {
  const target = useFileCommandTarget(path, targetOptions);
  return useFileCommandsForTarget(target, { onOpen, onOpenInNewTab, onOpenAsRoot, linkState });
}

/** The actions for an already resolved `target`. */
export function useFileCommandsForTarget(
  target: FileCommandTarget | null,
  { onOpen = null, onOpenInNewTab = null, onOpenAsRoot = null, linkState }: FileCommandOptions = {},
): FileCommandsForPath {
  const facts = useFileCommandFacts(target, {
    openable: onOpen !== null,
    newTabOpenable: onOpenInNewTab !== null,
    rootable: onOpenAsRoot !== null,
  });
  const commands = useMemo(() => (facts ? fileCommands(facts) : NO_COMMANDS), [facts]);
  const run = useCallback(
    (id: FileCommandId) => {
      if (id === "open") onOpen?.();
      else if (id === "open-in-new-tab") onOpenInNewTab?.();
      else if (id === "open-as-root") onOpenAsRoot?.();
      // Only a diff review knows a file's code host revision and old path; it runs those itself.
      else if (id === "open-on-code-host" || id === "copy-previous-path") return;
      else if (target) requestFileAction(target, id, { linkState: linkState?.() });
    },
    [target, onOpen, onOpenInNewTab, onOpenAsRoot, linkState],
  );
  return { target, commands, run };
}

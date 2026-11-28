// ⌘K's commands for a stacked PR's Changes: which changes they show, and Reload and Try Again
// while a comparison is stale or failed. The toolbar's menu by the file count runs the same ones.

import { useId, useMemo } from "react";

import { usePaletteCommands } from "@/store/paletteCommands";
import {
  reloadStackStatusCommand,
  retryStackComparisonCommand,
  stackDiffPaletteCommands,
} from "./stackDiffCommands";
import type { PullRequestStackDiff } from "./usePullRequestStackDiff";

const NO_COMMANDS: never[] = [];

/** ⌘K's choices while the PR's page is the active one. */
export function StackDiffPaletteCommands({
  diff,
  active,
  showChanges,
}: {
  diff: PullRequestStackDiff | null;
  active: boolean;
  showChanges: () => void;
}): null {
  const commands = useMemo(
    () => (diff && active ? stackDiffPaletteCommands(diff, showChanges) : NO_COMMANDS),
    [diff, active, showChanges],
  );
  const instance = useId();
  usePaletteCommands(`pull-request-stack-diff:${instance}`, "Pull request", commands);
  return null;
}

/**
 * ⌘K's Reload stack status while a comparison is stale, whether Changes or Summary read it,
 * and its Try Again after a chosen comparison failed, whichever renderer Changes uses.
 */
export function StackStatusPaletteCommands({
  diff,
  active,
}: {
  diff: PullRequestStackDiff | null;
  active: boolean;
}): null {
  const commands = useMemo(() => {
    if (!active) return NO_COMMANDS;
    const shown = [reloadStackStatusCommand(diff), retryStackComparisonCommand(diff)].flatMap(
      (command) => (command ? [command] : []),
    );
    return shown.length ? shown : NO_COMMANDS;
  }, [diff, active]);
  const instance = useId();
  usePaletteCommands(`pull-request-stack-status:${instance}`, "Pull request", commands);
  return null;
}

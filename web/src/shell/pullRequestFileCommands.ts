// A pull request's changed file as commands, from the shared changed-file
// definition so its label matches every other review surface.

import {
  type ChangedFileCommand,
  changedFileCommands,
} from "@/components/diff-review/changedFileCommands";

export type PullRequestFileCommand = ChangedFileCommand;

export interface PullRequestFileCommandFacts {
  /** The changed file the Changes tab shows, if it's the tab on screen. */
  shownPath: string | null;
  /** Files can open in a tab of their own, as in the tabs sidebar. */
  newTab: boolean;
}

/** The commands `facts` allows, in the order ⌘K lists them. */
export function pullRequestFileCommands(
  facts: PullRequestFileCommandFacts,
): PullRequestFileCommand[] {
  return changedFileCommands({ ...facts, sourceKeywords: ["pull request", "pr"] });
}

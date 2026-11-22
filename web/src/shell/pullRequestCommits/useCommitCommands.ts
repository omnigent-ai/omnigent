// The Commits tab's commands in ⌘K, and the same commands for the open commit's
// header buttons, from one definition.

import { useMemo, useRef, useId } from "react";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  FileDiffIcon,
  GitCommitHorizontalIcon,
  ListIcon,
  type LucideIcon,
  SquarePlusIcon,
} from "lucide-react";

import {
  isCommandAvailable,
  type PaletteCommand,
  usePaletteCommands,
} from "@/store/paletteCommands";

import {
  type CommitCommand,
  type CommitCommandFacts,
  type CommitCommandId,
  commitCommands,
} from "./commitCommands";

export const COMMIT_COMMAND_ICONS: Record<CommitCommandId, LucideIcon> = {
  "show-commits": GitCommitHorizontalIcon,
  "previous-commit": ChevronUpIcon,
  "next-commit": ChevronDownIcon,
  "all-commits": ListIcon,
  "all-changes": FileDiffIcon,
  "open-in-new-tab": SquarePlusIcon,
};

/** Where focus lands in Commits: its list, a step's button, or an opened commit's title. */
export type CommitFocusTarget = "list" | "previous-commit" | "next-commit" | "commit";

export interface CommitCommandHandlers {
  /** Opens the Commits tab on its list. */
  showCommits: () => void;
  /** Opens `sha`, a step in `direction` from the open commit. */
  openCommit: (sha: string, direction: "previous-commit" | "next-commit") => void;
  /** Leaves the open commit for the list. */
  showAllCommits: () => void;
  /** Opens the Changes tab. */
  showChanges: () => void;
  /** Opens `sha` in a tab of its own behind this one; offered only where `facts.newTab`. */
  openInNewTab?: (sha: string) => void;
}

export interface RunnableCommitCommand {
  command: CommitCommand;
  run: () => void;
}

const NO_COMMANDS: PaletteCommand[] = [];

/**
 * Offer `facts`' commands in ⌘K while `active`, and return them by id for the
 * header buttons. A row accepted after the open commit changed does nothing.
 */
export function useCommitCommands({
  conversationId,
  active,
  facts,
  handlers,
  heading = "Pull request",
}: {
  conversationId: string;
  /** False while the panel is hidden. */
  active: boolean;
  /** ⌘K's heading for the commands. */
  heading?: string;
  facts: CommitCommandFacts;
  handlers: CommitCommandHandlers;
}): Partial<Record<CommitCommandId, RunnableCommitCommand>> {
  // The panel makes new handlers each render; reading them late keeps the commands stable.
  const latest = useRef(handlers);
  latest.current = handlers;
  const { tabShown, onCommitsTab } = facts;
  const openSha = facts.open?.sha ?? null;
  const previousSha = facts.open?.previousSha ?? null;
  const nextSha = facts.open?.nextSha ?? null;
  const listed = facts.open?.listed ?? false;
  const moreAfterList = facts.open?.moreAfterList ?? false;
  const newTab = facts.newTab ?? false;
  const keywords = facts.keywords;
  const order = facts.order;
  const runnable = useMemo(() => {
    const open = openSha ? { sha: openSha, listed, previousSha, nextSha, moreAfterList } : null;
    const run = (command: CommitCommand) => () => {
      if (!isCommandAvailable(command.availability)) return;
      const current = latest.current;
      switch (command.id) {
        case "show-commits":
          return current.showCommits();
        case "previous-commit":
        case "next-commit":
          return command.targetSha ? current.openCommit(command.targetSha, command.id) : undefined;
        case "all-commits":
          return current.showAllCommits();
        case "all-changes":
          return current.showChanges();
        case "open-in-new-tab":
          return command.targetSha ? current.openInNewTab?.(command.targetSha) : undefined;
      }
    };
    return commitCommands({ tabShown, onCommitsTab, open, newTab, keywords, order }).map(
      (command) => ({
        command,
        run: run(command),
      }),
    );
  }, [
    tabShown,
    onCommitsTab,
    openSha,
    listed,
    previousSha,
    nextSha,
    moreAfterList,
    newTab,
    keywords,
    order,
  ]);
  const paletteCommands = useMemo<PaletteCommand[]>(
    () =>
      runnable.map(({ command, run }) => ({
        id: `${command.id}:${openSha ?? "list"}`,
        label: command.paletteLabel,
        icon: COMMIT_COMMAND_ICONS[command.id],
        keywords: [...command.keywords],
        availability: command.availability,
        movesFocus: command.movesFocus,
        run,
      })),
    [runnable, openSha],
  );
  const paletteInstance = useId();
  usePaletteCommands(
    `pull-request-commits:${conversationId}:${paletteInstance}`,
    heading,
    active ? paletteCommands : NO_COMMANDS,
  );
  return useMemo(
    () => Object.fromEntries(runnable.map((entry) => [entry.command.id, entry])),
    [runnable],
  );
}

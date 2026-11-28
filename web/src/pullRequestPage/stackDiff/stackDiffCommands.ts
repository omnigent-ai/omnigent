// The choices of which changes a stacked PR shows, defined once for its menu and ⌘K, so their
// labels and behavior can't drift apart.

import {
  GitCompareArrowsIcon,
  type LucideIcon,
  ListTreeIcon,
  RefreshCwIcon,
  RotateCcwIcon,
  WandSparklesIcon,
} from "lucide-react";

import type { DiffReviewBase } from "@/components/diff-review/DiffReviewBaseMenu";
import type { PaletteCommand } from "@/store/paletteCommands";
import { stackDiffFallbackMessage, type StackDiffIntent } from "./stackDiffMode";
import type { PullRequestStackDiff } from "./usePullRequestStackDiff";

export interface StackDiffChoice {
  intent: StackDiffIntent;
  /** The menu's name for the choice. */
  label: string;
  /** A line under it, about the PR `parent` names. */
  description: (parent: string) => string;
  /** ⌘K's command for it. */
  paletteLabel: string;
  keywords: readonly string[];
  icon: LucideIcon;
}

export const STACK_DIFF_CHOICES: readonly StackDiffChoice[] = [
  {
    intent: "automatic",
    label: "Automatic",
    description: (parent) => `Only this PR's changes when they can be told apart from ${parent}'s`,
    paletteLabel: "Choose the pull request diff automatically",
    keywords: ["stack", "diff", "changes", "automatic", "incremental"],
    icon: WandSparklesIcon,
  },
  {
    intent: "this-pr",
    label: "This PR only",
    description: (parent) => `Changes since ${parent}`,
    paletteLabel: "Show only this pull request's changes",
    keywords: ["stack", "diff", "changes", "incremental", "own", "parent", "since"],
    icon: GitCompareArrowsIcon,
  },
  {
    intent: "all",
    label: "All changes",
    description: () => "Everything since the base branch",
    paletteLabel: "Show all of the pull request's changes",
    keywords: ["stack", "diff", "changes", "base", "whole", "full"],
    icon: ListTreeIcon,
  },
];

export function stackDiffChoice(intent: StackDiffIntent): StackDiffChoice {
  return STACK_DIFF_CHOICES.find((choice) => choice.intent === intent) ?? STACK_DIFF_CHOICES[0]!;
}

/** What the Changes show now, in a sentence, for the toolbar's tooltip and messages. */
export function stackDiffStatusText(diff: PullRequestStackDiff): string {
  const { effective, parent } = diff;
  switch (effective.kind) {
    case "comparing":
      return `Comparing with ${parent.label}…`;
    case "stale":
      return "This stack changed since it was compared.";
    case "this-pr":
      return `Only this PR's changes, since ${parent.label}`;
    case "all":
      return effective.fallback
        ? stackDiffFallbackMessage(effective.fallback, parent.label)
        : "All changes since the base branch";
  }
}

/**
 * What Changes compare with, beside the file count: the parent where only this PR's changes
 * show, else the base branch, with the choices, Reload and Try Again in its menu.
 */
export function stackDiffReviewBase(diff: PullRequestStackDiff): DiffReviewBase {
  const actions = [reloadStackStatusCommand(diff), retryStackComparisonCommand(diff)].flatMap(
    (command) => (command ? [{ id: command.id, label: command.label, onSelect: command.run }] : []),
  );
  return {
    label: diff.effective.kind === "all" ? "vs base branch" : `vs ${diff.parent.label}`,
    description: stackDiffStatusText(diff),
    heading: "Show",
    choices: STACK_DIFF_CHOICES.map((choice) => ({
      id: choice.intent,
      label: choice.label,
      description: choice.description(diff.parent.label),
      checked: choice.intent === diff.intent,
      unavailableReason: null,
      onSelect: () => diff.choose(choice.intent),
    })),
    ...(actions.length ? { actions } : {}),
  };
}

/**
 * ⌘K's commands for the choices not already made. Each one also shows Changes, through
 * `showChanges`, so the reader sees what it chose.
 */
export function stackDiffPaletteCommands(
  diff: PullRequestStackDiff,
  showChanges: () => void,
): PaletteCommand[] {
  return STACK_DIFF_CHOICES.filter((choice) => choice.intent !== diff.intent).map((choice) => ({
    id: `pull-request-stack-diff.${choice.intent}`,
    label: choice.paletteLabel,
    description: choice.description(diff.parent.label),
    icon: choice.icon,
    keywords: [...choice.keywords],
    run: () => {
      diff.choose(choice.intent);
      showChanges();
    },
  }));
}

/**
 * Reload, for a comparison that went stale: it reads the PR and its stack again, then compares
 * afresh. Changes, Summary and ⌘K all run this one; null while nothing is stale.
 */
export function reloadStackStatusCommand(diff: PullRequestStackDiff | null): PaletteCommand | null {
  if (!diff) return null;
  const stale = diff.effective.kind === "stale" || diff.diagnostic?.kind === "stale";
  if (!stale) return null;
  return {
    id: "pull-request-stack-diff.reload",
    label: "Reload stack status",
    description: "Read this pull request and its stack again",
    icon: RefreshCwIcon,
    keywords: ["stack", "reload", "refresh", "stale", "parent"],
    run: diff.reload,
  };
}

/**
 * Try Again, after a chosen "This PR only" couldn't be compared: it compares with the parent
 * again. The toolbar's menu and ⌘K run this one; null unless the comparison failed.
 */
export function retryStackComparisonCommand(
  diff: PullRequestStackDiff | null,
): PaletteCommand | null {
  if (diff?.effective.kind !== "all" || diff.effective.fallback !== "failed") return null;
  return {
    id: "pull-request-stack-diff.retry",
    label: `Compare with ${diff.parent.label} again`,
    description: "Try again to show only this pull request's changes",
    icon: RotateCcwIcon,
    keywords: ["stack", "retry", "try again", "compare", "parent"],
    run: diff.retry,
  };
}

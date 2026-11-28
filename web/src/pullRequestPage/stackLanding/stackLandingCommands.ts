// What the merge area offers for landing a stack with its tool, defined once for its buttons and
// ⌘K, so their labels, availability and behavior can't drift apart.

import { ClipboardCopyIcon, GitMergeIcon, ListChecksIcon, RotateCcwIcon } from "lucide-react";
import { toast } from "sonner";

import { copyText } from "@/lib/clipboard";
import {
  type LandingPullRequest,
  type LandingStack,
  stackLandBranch,
  type StackLandingNotice,
  stackLandingMessage,
  stackLandingNotice,
  stackLandingTool,
} from "@/lib/pullRequestStacks/stackLanding";
import type { PaletteCommand } from "@/store/paletteCommands";
import { reloadStackStatusCommand } from "../stackDiff/stackDiffCommands";
import type { PullRequestStackDiff } from "../stackDiff/usePullRequestStackDiff";

/** A command, with the shorter name its button in the merge area shows. */
export interface StackLandingAction extends PaletteCommand {
  buttonLabel: string;
}

export interface StackLanding {
  notice: StackLandingNotice | null;
  /** What the merge area says for the notice. */
  message: string | null;
  /** Copy the command that lands every PR before this one, then this one; null where it can't. */
  land: StackLandingAction | null;
  /** Copy the same command, only checking each PR could land. */
  check: StackLandingAction | null;
  /** Copy the command that drops a merged parent's commits from this PR. */
  sync: StackLandingAction | null;
  /** Read the PR and its stack again, for a check that went stale. */
  reload: StackLandingAction | null;
  /** Ask a failed check again. */
  retry: StackLandingAction | null;
}

function copying(value: string, what: string): () => void {
  return () => {
    void copyText(value).then(
      () => toast.success(`Copied the ${what}`),
      () => toast.error(`Couldn’t copy the ${what}`),
    );
  };
}

/** Everything the merge area and ⌘K offer for landing `pr`'s stack. */
export function stackLanding(
  stack: LandingStack | null,
  pr: LandingPullRequest | null,
  diff: PullRequestStackDiff | null,
): StackLanding {
  const tool = stack ? stackLandingTool(stack.parse) : null;
  const notice = tool ? stackLandingNotice(stack, pr, diff?.diagnostic ?? null) : null;
  const branch = tool ? stackLandBranch(stack, pr) : null;
  const reload = notice?.kind === "stale" ? reloadStackStatusCommand(diff) : null;
  return {
    notice,
    message: notice && tool ? stackLandingMessage(notice, tool) : null,
    land:
      tool && branch
        ? {
            id: "pull-request-stack-land.copy",
            label: `Copy ${tool.landName} command`,
            buttonLabel: "Copy land command",
            description: `Lands every PR before this one in the stack, in order, then this one. Run it in your ${tool.name} checkout of this stack.`,
            icon: GitMergeIcon,
            keywords: ["stack", tool.name, "land", "merge", "copy", "command"],
            run: copying(tool.landCommand(branch, { check: false }), "land command"),
          }
        : null,
    check:
      tool && branch
        ? {
            id: "pull-request-stack-land.copy-check",
            label: `Copy ${tool.checkName} command`,
            buttonLabel: "Copy check command",
            description: `Checks approval, pushes and CI for this PR and every PR before it, without landing anything. Run it in your ${tool.name} checkout of this stack.`,
            icon: ListChecksIcon,
            keywords: ["stack", tool.name, "land", "check", "copy", "command"],
            run: copying(tool.landCommand(branch, { check: true }), "check command"),
          }
        : null,
    sync:
      tool && notice?.kind === "sync"
        ? {
            id: "pull-request-stack-land.copy-sync",
            label: `Copy ${tool.syncName} command`,
            buttonLabel: "Copy command",
            description: `Drops ${notice.parent.label}'s commits from this PR, then pushes it`,
            icon: ClipboardCopyIcon,
            keywords: ["stack", tool.name, "sync", "push", "copy", "command"],
            run: copying(tool.syncCommand, "sync command"),
          }
        : null,
    reload: reload && { ...reload, buttonLabel: "Reload stack status" },
    retry:
      notice?.kind === "failed" && diff
        ? {
            id: "pull-request-stack-land.retry",
            label: "Check the stack's parent again",
            buttonLabel: "Try Again",
            description: `Check again whether this PR still includes ${notice.parent.label}'s commits`,
            icon: RotateCcwIcon,
            keywords: ["stack", "retry", "again", "parent", "sync"],
            run: diff.retry,
          }
        : null,
  };
}

/** ⌘K's landing commands. Reload stack status comes with the stack diff's own commands. */
export function stackLandingPaletteCommands(landing: StackLanding): PaletteCommand[] {
  return [landing.land, landing.check, landing.sync, landing.retry]
    .filter((action): action is StackLandingAction => !!action)
    .map(({ buttonLabel: _button, ...command }) => command);
}

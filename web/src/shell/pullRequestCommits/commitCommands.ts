// The Commits tab's navigation as commands: ⌘K and the open commit's header
// buttons take their labels and availability from here, so they can't drift.
// Plain facts in, no React.

import { AVAILABLE, type CommandAvailability, unavailableBecause } from "@/store/paletteCommands";

export type CommitCommandId =
  | "show-commits"
  | "previous-commit"
  | "next-commit"
  | "all-commits"
  | "all-changes"
  | "open-in-new-tab";

export const FIRST_COMMIT_REASON = "This is the first commit.";
export const LAST_COMMIT_REASON = "This is the last commit.";
export const NEWEST_COMMIT_REASON = "This is the newest commit.";
export const OLDEST_COMMIT_REASON = "This is the oldest commit.";
/** The list stops short of the PR's commits, so its last row may not be the PR's last. */
export const LAST_SHOWN_COMMIT_REASON = "This is the last commit shown here.";
/** The open commit isn't in the list, so it has no neighbours to step to. */
export const UNLISTED_COMMIT_REASON = "This commit isn’t in the list shown here.";

export interface CommitCommandFacts {
  /** The PR shows a Commits tab at all. */
  tabShown: boolean;
  /** The Commits tab is the one on screen. */
  onCommitsTab: boolean;
  /** The commit open in the tab, with its neighbours in the list. */
  open: OpenCommitFacts | null;
  /** Commits can open in a tab of their own, as in the tabs sidebar. */
  newTab?: boolean;
  /** What ⌘K matches every command by; a pull request's words unless given. */
  keywords?: readonly string[];
  /** How the list runs, which says whether the previous commit is older; oldest first unless given. */
  order?: "oldest-first" | "newest-first";
}

export interface OpenCommitFacts {
  sha: string;
  /** The list includes the open commit; otherwise it has no known neighbours. */
  listed: boolean;
  previousSha: string | null;
  nextSha: string | null;
  /** The PR has commits past the end of the list, such as beyond a host's limit. */
  moreAfterList: boolean;
}

export interface CommitCommand {
  id: CommitCommandId;
  /** The header button's name. */
  buttonLabel: string;
  paletteLabel: string;
  keywords: readonly string[];
  availability: CommandAvailability;
  /** Takes focus somewhere new, so ⌘K runs it once it has closed. */
  movesFocus: boolean;
  /** The commit it opens: for previous and next, and the open one for a new tab. */
  targetSha?: string;
}

const PULL_REQUEST_KEYWORDS = ["commit", "pull request", "pr", "history"];
/** A branch's own commits, as the Diffs view lists them. */
export const BRANCH_COMMIT_KEYWORDS = ["commit", "branch", "history", "diff"];

/** The commands `facts` allows, in the order ⌘K lists them. */
export function commitCommands(facts: CommitCommandFacts): CommitCommand[] {
  if (!facts.tabShown) return [];
  const keywords = facts.keywords ?? PULL_REQUEST_KEYWORDS;
  const newestFirst = facts.order === "newest-first";
  if (!facts.onCommitsTab) {
    return [
      {
        id: "show-commits",
        buttonLabel: "Commits",
        paletteLabel: "Show commits",
        keywords: [...keywords, "tab", "list"],
        availability: AVAILABLE,
        movesFocus: true,
      },
    ];
  }
  const { open } = facts;
  if (!open) return [];
  const firstReason = !open.listed
    ? UNLISTED_COMMIT_REASON
    : newestFirst
      ? NEWEST_COMMIT_REASON
      : FIRST_COMMIT_REASON;
  const lastReason = !open.listed
    ? UNLISTED_COMMIT_REASON
    : open.moreAfterList
      ? LAST_SHOWN_COMMIT_REASON
      : newestFirst
        ? OLDEST_COMMIT_REASON
        : LAST_COMMIT_REASON;
  const commands: CommitCommand[] = [
    {
      id: "previous-commit",
      buttonLabel: "Previous commit",
      paletteLabel: "Go to the previous commit",
      keywords: [...keywords, newestFirst ? "newer" : "older", "back", "up"],
      availability: open.previousSha ? AVAILABLE : unavailableBecause(firstReason),
      // The commit's review is replaced, so focus goes to its header once ⌘K closes.
      movesFocus: true,
      ...(open.previousSha && { targetSha: open.previousSha }),
    },
    {
      id: "next-commit",
      buttonLabel: "Next commit",
      paletteLabel: "Go to the next commit",
      keywords: [...keywords, newestFirst ? "older" : "newer", "forward", "down"],
      availability: open.nextSha ? AVAILABLE : unavailableBecause(lastReason),
      movesFocus: true,
      ...(open.nextSha && { targetSha: open.nextSha }),
    },
    {
      id: "all-commits",
      buttonLabel: "All commits",
      paletteLabel: "Show all commits",
      keywords: [...keywords, "list", "back"],
      availability: AVAILABLE,
      movesFocus: true,
    },
    {
      id: "all-changes",
      buttonLabel: "All changes",
      paletteLabel: "Show all changes",
      keywords: [...keywords, "changes", "diff", "files"],
      availability: AVAILABLE,
      movesFocus: true,
    },
  ];
  if (facts.newTab) {
    // Opens behind this tab, as ⌘-click on a commit row does; focus stays here.
    commands.push({
      id: "open-in-new-tab",
      buttonLabel: "Open in new tab",
      paletteLabel: `Open commit ${open.sha.slice(0, 7)} in a new tab`,
      keywords: [...keywords, "tab", "new", "background"],
      availability: AVAILABLE,
      movesFocus: false,
      targetSha: open.sha,
    });
  }
  return commands;
}

// The Diffs view's open commit: a toolbar naming it, with ← Commits and ↑/↓ through
// the branch's commits, over what that commit changed. It's read through a comparison
// owner of its own, so the session's comparison and every other view of it stay as they were.

import { ArrowLeftIcon, ChevronDownIcon, ChevronUpIcon, Loader2Icon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { ComparisonOwnerContext, useComparisonOwner } from "@/hooks/useComparisonOwner";
import { type WorkingCopyChanges, useWorkingCopyChanges } from "@/hooks/useWorkingCopyChanges";
import type { BranchCommit } from "@/lib/branchCommits";
import { type CommitComparisons, createCommitComparisons } from "@/lib/commitComparisons";
import {
  commitRevisions,
  type RevisionSpec,
  sameRevisions,
  shortCommit,
} from "@/lib/customComparison";
import { relativeTime } from "@/lib/relativeTime";
import type { WorkingCopyComparisonOwner } from "@/lib/workingCopyComparisonOwner";
import { createComparisonOwner } from "@/lib/workingCopyComparisons";
import { IconButton } from "./PullRequestPanelControls";
import type { CommitCommandId } from "./pullRequestCommits/commitCommands";
import type { RunnableCommitCommand } from "./pullRequestCommits/useCommitCommands";

export const COMMIT_CHANGES_UNAVAILABLE = "Couldn't read this commit's changes.";

/** A step through the list that keeps focus in the toolbar; each new object is one request. */
export interface CommitStepFocus {
  target: "previous-commit" | "next-commit" | "commit";
}

/**
 * The owner that reads `spec`, made once a commit is first opened and kept for the
 * review's life, so stepping only changes what it compares.
 */
export function useCommitComparisons(spec: RevisionSpec | null): WorkingCopyComparisonOwner | null {
  const instance = useRef<CommitComparisons | null>(null);
  const [owner, setOwner] = useState<WorkingCopyComparisonOwner | null>(null);
  const base = spec?.base;
  const head = spec?.head;
  useEffect(() => {
    if (base === undefined || head === undefined) return;
    const next = { base, head };
    if (instance.current) {
      instance.current.show(next);
      return;
    }
    const created = createCommitComparisons(next, (customComparisons) =>
      createComparisonOwner({ customComparisons, onlyTyped: true }),
    );
    instance.current = created;
    setOwner(created.owner);
  }, [base, head]);
  useEffect(
    () => () => {
      instance.current?.dispose();
      instance.current = null;
    },
    [],
  );
  return owner;
}

/** One commit of the branch, opened from the Commits tab. */
export function OpenBranchCommit({
  conversationId,
  commit,
  position,
  commands,
  focusRequest,
  renderChanges,
}: {
  conversationId: string;
  commit: BranchCommit;
  /** Its place in the listed commits, newest first. */
  position: { index: number; count: number };
  commands: Partial<Record<CommitCommandId, RunnableCommitCommand>>;
  focusRequest: CommitStepFocus | null;
  /** The review of the commit's changes, once they're listed. */
  renderChanges: (changes: WorkingCopyChanges) => React.ReactNode;
}) {
  const spec = commitRevisions(commit.oid, { root: commit.root });
  const owner = useCommitComparisons(spec);
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="changes-review-open-commit">
      <BranchCommitToolbar
        commit={commit}
        position={position}
        commands={commands}
        focusRequest={focusRequest}
      />
      {owner ? (
        <ComparisonOwnerContext.Provider value={owner}>
          <CommitChanges conversationId={conversationId} spec={spec} render={renderChanges} />
        </ComparisonOwnerContext.Provider>
      ) : (
        <CommitChangesMessage busy message="Loading commit changes…" />
      )}
    </div>
  );
}

/**
 * The open commit's title, author and short sha, with ← Commits and its place in the
 * list. ↑ and ↓ step through the list while focus is anywhere in it.
 */
export function BranchCommitToolbar({
  commit,
  position,
  commands,
  focusRequest,
}: {
  commit: BranchCommit;
  position: { index: number; count: number };
  commands: Partial<Record<CommitCommandId, RunnableCommitCommand>>;
  focusRequest: CommitStepFocus | null;
}) {
  const toolbarRef = useRef<HTMLDivElement>(null);
  const titleRef = useRef<HTMLHeadingElement>(null);
  // After a step the toolbar stays, so focus stays too, unless its button can't go further:
  // then it goes to the other direction's button, else the title.
  useEffect(() => {
    const target = focusRequest?.target;
    if (!target) return;
    if (target === "commit") {
      titleRef.current?.focus();
      return;
    }
    const order: CommitCommandId[] =
      target === "previous-commit"
        ? ["previous-commit", "next-commit"]
        : ["next-commit", "previous-commit"];
    const button = order
      .map((id) =>
        toolbarRef.current?.querySelector<HTMLButtonElement>(
          `[data-commit-command="${id}"] button`,
        ),
      )
      .find((candidate) => candidate && !candidate.disabled);
    (button ?? titleRef.current)?.focus();
  }, [focusRequest]);
  const step = (id: "previous-commit" | "next-commit") => {
    const entry = commands[id];
    if (!entry?.command.availability.available) return false;
    entry.run();
    return true;
  };
  const subject = commit.subject || shortCommit(commit.oid);
  return (
    <div
      ref={toolbarRef}
      role="group"
      aria-label="Commit"
      className="grid shrink-0 grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-x-2 border-b border-border py-1.5 pr-2 pl-1"
      data-testid="changes-review-commit-toolbar"
      onKeyDown={(event) => {
        if (event.altKey || event.metaKey || event.ctrlKey || event.shiftKey) return;
        const id =
          event.key === "ArrowUp"
            ? "previous-commit"
            : event.key === "ArrowDown"
              ? "next-commit"
              : null;
        if (id && step(id)) event.preventDefault();
      }}
    >
      <CommitBackButton entry={commands["all-commits"]} />
      <div className="flex min-w-0 flex-col gap-0.5">
        <h3
          ref={titleRef}
          tabIndex={-1}
          className="truncate text-ui font-semibold outline-none"
          title={subject}
        >
          {subject}
        </h3>
        <span className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
          {commit.authorName && <span className="truncate">{commit.authorName}</span>}
          {commit.authorName && <span aria-hidden="true">·</span>}
          <code className="shrink-0 font-mono">{shortCommit(commit.oid)}</code>
          {commit.authoredAtMs !== null && (
            <>
              <span aria-hidden="true">·</span>
              <time dateTime={new Date(commit.authoredAtMs).toISOString()} className="shrink-0">
                {relativeTime(commit.authoredAtMs)}
              </time>
            </>
          )}
        </span>
        {/* The commit's message goes here once the listing includes it. */}
      </div>
      <span
        className="text-xs whitespace-nowrap text-muted-foreground tabular-nums @max-[360px]/workspace-header:hidden"
        data-testid="changes-review-commit-position"
      >
        {position.index + 1} of {position.count}
      </span>
      <div className="flex items-center gap-0.5" role="group" aria-label="Commit navigation">
        <CommitStepButton entry={commands["previous-commit"]} icon={ChevronUpIcon} />
        <CommitStepButton entry={commands["next-commit"]} icon={ChevronDownIcon} />
      </div>
    </div>
  );
}

/** "← Commits": back to the list, through the shared Show all commits command. */
function CommitBackButton({ entry }: { entry: RunnableCommitCommand | undefined }) {
  if (!entry) return null;
  return (
    <span data-commit-command={entry.command.id} className="contents">
      <Button
        type="button"
        variant="ghost"
        size="xs"
        className="text-primary hover:text-primary"
        title={entry.command.buttonLabel}
        onClick={entry.run}
      >
        <ArrowLeftIcon aria-hidden="true" />
        Commits
      </Button>
    </span>
  );
}

/** ↑ or ↓, named and disabled as ⌘K shows its command. */
function CommitStepButton({
  entry,
  icon: Icon,
}: {
  entry: RunnableCommitCommand | undefined;
  icon: typeof ChevronUpIcon;
}) {
  if (!entry) return null;
  const { command, run } = entry;
  const blocked = command.availability.available ? undefined : command.availability.reason;
  return (
    // Named, so a step that can't go further moves focus to the other button.
    <span data-commit-command={command.id} className="contents">
      <IconButton
        label={blocked ? `${command.buttonLabel}: ${blocked}` : command.buttonLabel}
        disabled={!!blocked}
        onClick={run}
        className="text-muted-foreground"
      >
        <Icon className="size-3.5" />
      </IconButton>
    </span>
  );
}

/** The commit's changes from the owner above, once it lists this commit. */
function CommitChanges({
  conversationId,
  spec,
  render,
}: {
  conversationId: string;
  spec: RevisionSpec;
  render: (changes: WorkingCopyChanges) => React.ReactNode;
}) {
  // A commit's changes don't move, so this view never polls.
  const changes = useWorkingCopyChanges(conversationId, { kind: "file" });
  const owner = useComparisonOwner();
  const { comparison } = changes;
  // Until the owner compares this commit, anything listed is the previous one's.
  if (!sameRevisions(changes.custom, spec) || comparison.base === "pending") {
    return <CommitChangesMessage busy message="Loading commit changes…" />;
  }
  if (comparison.base === "unavailable" || (changes.isError && !changes.data)) {
    return (
      <CommitChangesMessage
        alert
        title="Commit Changes Unavailable"
        message={COMMIT_CHANGES_UNAVAILABLE}
        onRetry={() => owner.retry(conversationId)}
      />
    );
  }
  if (comparison.base !== "branch-point" || !changes.data) {
    return <CommitChangesMessage busy message="Loading commit changes…" />;
  }
  return render(changes);
}

function CommitChangesMessage({
  title,
  message,
  busy = false,
  alert = false,
  onRetry,
}: {
  title?: string;
  message: string;
  busy?: boolean;
  alert?: boolean;
  onRetry?: () => void;
}) {
  return (
    <div
      role={alert ? "alert" : "status"}
      className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-ui"
      data-testid="changes-review-commit-message"
    >
      {title && <strong className="font-semibold">{title}</strong>}
      <span className="flex items-center gap-2 text-muted-foreground">
        {busy && <Loader2Icon aria-hidden="true" className="size-4 animate-spin" />}
        {message}
      </span>
      {onRetry && (
        <Button type="button" size="sm" variant="outline" onClick={onRetry}>
          Try Again
        </Button>
      )}
    </div>
  );
}

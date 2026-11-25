// A stacked PR's own changes since its parent, on the shared diff review, only to read: no
// conversations, drafts or viewed marks, which stay with All changes. Gaps in what the shell
// could prove say so, and point to All changes.

import { SearchIcon } from "lucide-react";
import { useCallback, useMemo, useRef, useState } from "react";

import { DiffReview, type DiffReviewSourceLayer } from "@/components/diff-review/DiffReview";
import type { DiffReviewBase } from "@/components/diff-review/DiffReviewBaseMenu";
import type { DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import { Button } from "@/components/ui/button";
import { useSavedWrapLines } from "@/hooks/useSavedWrapLines";
import { useFindSurface } from "@/lib/findRouting";
import type { StackComparison } from "@/lib/pullRequestStacks/stackCompare";
import { FIND_IN_CHANGES_LABEL } from "@/components/diff-review/diffReviewCommands";
import { PULL_REQUEST_FIND_HIGHLIGHTS } from "@/components/diff-review/findHighlight";
import { IconButton } from "@/shell/PullRequestPanelControls";
import {
  type StackRepositories,
  stackComparisonFileActions,
} from "@/shell/pullRequestReview/pullRequestFileActions";
import { useReviewLineSelection } from "@/shell/pullRequestReview/useReviewLineSelection";
import { stackCompareReview } from "./stackCompareReview";

export interface PullRequestStackOnlyReviewProps {
  comparison: StackComparison;
  /** The PR this one is stacked on, such as `#100`. */
  parentLabel: string;
  conversationId: string;
  prUrl: string;
  provider?: string | null;
  /** Where the PR's and its parent's commits live, for its files' code host links. */
  repositories?: StackRepositories;
  /** Conversations on the PR, which only All changes shows. */
  conversationCount: number;
  onShowAllChanges: () => void;
  /** A file a tab's place or another view asked for, revealed when this PR changed it. */
  revealPath?: string | null;
  onOpenFileInNewTab?: (path: string, options: { background: boolean }) => void;
  layout: "unified" | "split";
  onLayoutChange: (layout: "unified" | "split") => void;
  active?: boolean;
  /** What these changes compare with, and the menu to choose others, beside the file count. */
  base?: DiffReviewBase | null;
}

/** Where this comparison's per-file choices live, apart from the PR's own review. */
export function stackOnlyReviewScope(prUrl: string, comparison: StackComparison): string {
  return `pull-request:${prUrl}:stack:${comparison.parentHead}..${comparison.childHead}`;
}

function conversationsNote(count: number): string | null {
  if (count === 0) return null;
  return count === 1
    ? "1 conversation is in All changes."
    : `${count} conversations are in All changes.`;
}

export function PullRequestStackOnlyReview({
  comparison,
  parentLabel,
  conversationId,
  prUrl,
  provider,
  repositories,
  conversationCount,
  onShowAllChanges,
  revealPath = null,
  onOpenFileInNewTab,
  layout,
  onLayoutChange,
  active = true,
  base = null,
}: PullRequestStackOnlyReviewProps) {
  const review = useMemo(() => stackCompareReview(comparison), [comparison]);
  // A file asked for that only the parent changed is in All changes, not here.
  const requestedHere = revealPath !== null && review.contents.has(revealPath);
  const requestedElsewhere = revealPath !== null && !requestedHere;
  const [wrapLines, changeWrapLines] = useSavedWrapLines();

  const [findOpen, setFindOpen] = useState(false);
  const [findFocusRequest, setFindFocusRequest] = useState(0);
  const openFind = useCallback(() => {
    setFindOpen(true);
    setFindFocusRequest((request) => request + 1);
  }, []);
  const shell = useRef<HTMLDivElement | null>(null);
  const isFindActive = useCallback(
    () => shell.current?.contains(document.activeElement) ?? false,
    [],
  );
  useFindSurface({ isActive: isFindActive, openFind });

  // Picked lines only point the file's code host link at them, on the side they're on.
  const lineSelection = useReviewLineSelection(`${comparison.parentHead}..${comparison.childHead}`);
  const { committed, shown, onLineSelection } = lineSelection;
  const selectedLines = useMemo(
    () => (shown ? { path: shown.path, range: shown.range } : null),
    [shown],
  );
  // Keyed by the values, not the object the panel builds each render; absent keys stay absent.
  const hasHeadRepository = !!repositories && "headRepositoryUrl" in repositories;
  const headRepositoryUrl = repositories?.headRepositoryUrl;
  const hasParentRepository = !!repositories && "parentHeadRepository" in repositories;
  const parentHeadRepository = repositories?.parentHeadRepository;
  const stackRepositories = useMemo(
    (): StackRepositories => ({
      ...(hasHeadRepository ? { headRepositoryUrl } : {}),
      ...(hasParentRepository ? { parentHeadRepository } : {}),
    }),
    [hasHeadRepository, headRepositoryUrl, hasParentRepository, parentHeadRepository],
  );
  const fileActions = useCallback(
    (file: DiffReviewFile) =>
      stackComparisonFileActions(
        file,
        review.previousPaths.get(file.path) ?? null,
        {
          conversationId,
          prUrl,
          provider,
          childHead: comparison.childHead,
          parentHead: comparison.parentHead,
          repositories: stackRepositories,
        },
        committed,
      ),
    [review, conversationId, prUrl, provider, comparison, stackRepositories, committed],
  );
  const source: DiffReviewSourceLayer<undefined> = {
    // Upstream's names for a pull request's find, kept apart from the session review's.
    findHighlightNames: PULL_REQUEST_FIND_HIGHLIGHTS,
    onLineSelection,
    selectedLines,
    toolbar: (
      <IconButton
        label={FIND_IN_CHANGES_LABEL}
        onClick={openFind}
        className="size-7 text-muted-foreground"
      >
        <SearchIcon className="size-4" />
      </IconButton>
    ),
  };

  const notes = [
    requestedElsewhere ? `${revealPath} is only in All changes, from ${parentLabel}.` : null,
    comparison.filesTruncated ? "Showing the first 300 files. All changes may list more." : null,
    comparison.patchesTruncated
      ? "Some diffs aren't available here. All changes shows them."
      : null,
    conversationsNote(conversationCount),
  ].filter((note): note is string => note !== null);

  return (
    <div
      ref={shell}
      className="flex min-h-0 flex-1 flex-col"
      onPointerCancel={lineSelection.cancelPreview}
    >
      {notes.length > 0 && (
        <div
          data-testid="pull-request-stack-only-notes"
          className="flex shrink-0 items-center gap-2 border-b border-border px-2 py-1 text-ui text-muted-foreground"
        >
          <span className="min-w-0 flex-1">{notes.join(" ")}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-6"
            onClick={onShowAllChanges}
          >
            Show all changes
          </Button>
        </div>
      )}
      <div className="min-h-0 flex-1">
        <DiffReview
          diagnosticsView="pull_request_stack"
          files={review.files}
          contents={review.contents}
          onRetry={() => undefined}
          layout={layout}
          onLayoutChange={onLayoutChange}
          wrapLines={wrapLines}
          onWrapLinesChange={changeWrapLines}
          commandHeading="This PR's changes"
          commandsActive={active}
          findOpen={findOpen}
          findFocusRequest={findFocusRequest}
          onFindClose={() => setFindOpen(false)}
          emptyMessage={`This PR doesn't change any files since ${parentLabel}.`}
          source={source}
          fileActions={fileActions}
          reviewScope={stackOnlyReviewScope(prUrl, comparison)}
          revealPath={requestedHere ? revealPath : null}
          onOpenFileInNewTab={onOpenFileInNewTab}
          viewedMarks={false}
          base={base}
        />
      </div>
    </div>
  );
}

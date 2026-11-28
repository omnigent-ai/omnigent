// One commit of a pull request on the shared diff review: the same tree,
// filters, find, layout, and file actions as the PR's Changes review, but only
// to read. No conversations, drafts, or composers, and viewed marks kept per commit.

import { useCallback, useMemo, useRef, useState } from "react";
import type { FileDiffMetadata } from "@pierre/diffs";
import { SearchIcon } from "lucide-react";
import { DiffReview, type DiffReviewSourceLayer } from "@/components/diff-review/DiffReview";
import type { DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import type { PullRequestChangedFile } from "@/newPullRequestPage/hooks/usePullRequests";
import { useSavedWrapLines } from "@/hooks/useSavedWrapLines";
import { useFindSurface } from "@/lib/findRouting";
import { FIND_IN_CHANGES_LABEL } from "@/components/diff-review/diffReviewCommands";
import { PULL_REQUEST_FIND_HIGHLIGHTS } from "@/components/diff-review/findHighlight";
import { IconButton } from "./PullRequestPanelControls";
import {
  pullRequestFileActions,
  pullRequestFolderActions,
} from "./pullRequestReview/pullRequestFileActions";
import { useReviewLineSelection } from "./pullRequestReview/useReviewLineSelection";
import { pullRequestReviewContents, pullRequestReviewFile } from "./pullRequestReviewSource";

export interface PullRequestCommitReviewProps {
  files: readonly PullRequestChangedFile[];
  filesByPath: ReadonlyMap<string, FileDiffMetadata>;
  conversationId: string;
  prUrl: string;
  /** The PR's git provider id; absent from hosts that predate providers (GitHub). */
  provider?: string | null;
  /** The commit the files come from. */
  sha: string;
  /**
   * Where the PR's commits live, such as a fork, as `head_repository_url`; a key absent from
   * an older host stays absent, so the PR's own repository is used, and null is unknown.
   */
  repositories?: { headRepositoryUrl?: string | null };
  layout: "unified" | "split";
  onLayoutChange: (layout: "unified" | "split") => void;
  /** False while the panel is mounted but hidden; its ⌘K commands are withdrawn. */
  active?: boolean;
}

/** Where a commit's viewed marks live, apart from the pull request's own. */
export function commitReviewScope(prUrl: string, sha: string): string {
  return `pull-request:${prUrl}:commit:${sha}`;
}

export function PullRequestCommitReview({
  files,
  filesByPath,
  conversationId,
  prUrl,
  provider,
  sha,
  repositories,
  layout,
  onLayoutChange,
  active = true,
}: PullRequestCommitReviewProps) {
  const reviewFiles = useMemo(
    () => files.map((file) => pullRequestReviewFile(file, filesByPath.get(file.path))),
    [files, filesByPath],
  );
  const contents = useMemo(
    () => pullRequestReviewContents(files, filesByPath),
    [files, filesByPath],
  );
  const [wrapLines, changeWrapLines] = useSavedWrapLines();

  const [findOpen, setFindOpen] = useState(false);
  const [findFocusRequest, setFindFocusRequest] = useState(0);
  const openFind = useCallback(() => {
    setFindOpen(true);
    setFindFocusRequest((request) => request + 1);
  }, []);
  const shell = useRef<HTMLDivElement | null>(null);
  // ⌘F and the desktop Edit > Find… open the review's find while the reader works in it.
  const isFindActive = useCallback(
    () => shell.current?.contains(document.activeElement) ?? false,
    [],
  );
  useFindSurface({ isActive: isFindActive, openFind });

  // Picked lines only point the file's code host link at them.
  // Keyed by the values, not the object the panel builds each render.
  const hasHeadRepository = !!repositories && "headRepositoryUrl" in repositories;
  const headRepositoryUrl = repositories?.headRepositoryUrl;
  const lineSelection = useReviewLineSelection(sha);
  const { committed, shown, onLineSelection } = lineSelection;
  const selectedLines = useMemo(
    () => (shown ? { path: shown.path, range: shown.range } : null),
    [shown],
  );
  const fileActions = useCallback(
    (file: DiffReviewFile) =>
      pullRequestFileActions(
        file,
        {
          conversationId,
          prUrl,
          provider,
          sha,
          ...(hasHeadRepository ? { headRepositoryUrl } : {}),
        },
        committed,
      ),
    [conversationId, prUrl, provider, sha, hasHeadRepository, headRepositoryUrl, committed],
  );
  const folderActions = useCallback(
    (path: string) => pullRequestFolderActions(path, conversationId),
    [conversationId],
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

  return (
    <div ref={shell} className="min-h-0 flex-1" onPointerCancel={lineSelection.cancelPreview}>
      <DiffReview
        diagnosticsView="pull_request_commit"
        files={reviewFiles}
        contents={contents}
        onRetry={() => undefined}
        layout={layout}
        onLayoutChange={onLayoutChange}
        wrapLines={wrapLines}
        onWrapLinesChange={changeWrapLines}
        commandHeading="Commit"
        commandsActive={active}
        findOpen={findOpen}
        findFocusRequest={findFocusRequest}
        onFindClose={() => setFindOpen(false)}
        emptyMessage="This commit doesn’t change any files."
        source={source}
        fileActions={fileActions}
        folderActions={folderActions}
        reviewScope={commitReviewScope(prUrl, sha)}
      />
    </div>
  );
}

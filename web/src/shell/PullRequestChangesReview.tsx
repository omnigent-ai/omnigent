// A pull request's Changes tab on the shared diff review: the same tree,
// filters, find, and layout as the local review, with the pull request's review
// conversations and new-conversation drafts as rows under their lines.

import { Badge } from "@/components/ui/badge";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type {
  DiffLineAnnotation,
  FileDiffContentsLoader,
  FileDiffMetadata,
  SelectedLineRange,
} from "@pierre/diffs";
import { ChevronDownIcon, ChevronUpIcon, MessageSquareIcon, SearchIcon } from "lucide-react";
import {
  DiffReview,
  type DiffReviewHoveredLine,
  type DiffReviewSourceLayer,
} from "@/components/diff-review/DiffReview";
import type { DiffReviewBase } from "@/components/diff-review/DiffReviewBaseMenu";
import type { DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import {
  DiffReviewToolbar,
  DiffReviewToolbarProgress,
} from "@/components/diff-review/DiffReviewToolbar";
import type { DiffReviewPresentation } from "@/components/diff-review/reviewPresentation";
import {
  PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS,
  RecordedReviewToolbar,
} from "@/components/diff-review/reviewToolbarHeight";
import { PULL_REQUEST_REVIEW_TREE_WIDTH } from "@/components/diff-review/reviewTreeWidth";
import type {
  ReviewRevealTarget,
  ReviewScrollAnchor,
} from "@/components/diff-review/reviewNavigation";
import {
  useNewReviewThreadDrafts,
  usePullRequestReviewThreadComposer,
  type PullRequestChangedFile,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { useSavedWrapLines } from "@/hooks/useSavedWrapLines";
import { useFindSurface } from "@/lib/findRouting";
import { reviewSelection, type ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  conversationRange,
  NewConversationButton,
  NewConversationComposer,
  SELECTION_OUTSIDE_ONE_HUNK,
} from "./PullRequestNewConversation";
import { FIND_IN_CHANGES_LABEL } from "@/components/diff-review/diffReviewCommands";
import { PULL_REQUEST_FIND_HIGHLIGHTS } from "@/components/diff-review/findHighlight";
import { IconButton } from "./PullRequestPanelControls";
import {
  pullRequestFileActions,
  pullRequestFolderActions,
} from "./pullRequestReview/pullRequestFileActions";
import { useReviewLineSelection } from "./pullRequestReview/useReviewLineSelection";
import {
  PullRequestReviewThread,
  type ReviewThreadContext,
} from "../newPullRequestPage/shell/PullRequestReviewThread";
import {
  keepUnchangedAnnotations,
  pullRequestReviewAnnotations,
  pullRequestReviewContents,
  pullRequestReviewFile,
  reviewThreadLocation,
  type PullRequestPatchState,
  type PullRequestReviewAnnotation,
  type PullRequestReviewAnnotations,
  type ReviewDraftPlacement,
} from "./pullRequestReviewSource";

const NO_DRAFT_PATHS: readonly string[] = [];
// A changed file's tree row keeps its menu (Open on GitHub, …) where nothing opens new tabs.
// The toolbar records its height, so the page's placeholder leaves the room it wraps to.
const PRESENTATION: DiffReviewPresentation = {
  treeRowActions: true,
  treeColumn: PULL_REQUEST_REVIEW_TREE_WIDTH,
  toolbar: (toolbar) => (
    <RecordedReviewToolbar heights={PULL_REQUEST_REVIEW_TOOLBAR_HEIGHTS}>
      <DiffReviewToolbar {...toolbar} />
    </RecordedReviewToolbar>
  ),
};

/** Conversations, and drafts, no file's diff shows, after the last file. */
export function EarlierConversations({
  threads,
  context,
  draftPaths = NO_DRAFT_PATHS,
}: {
  threads: readonly ReviewThread[];
  context: ReviewThreadContext;
  /** Files with a saved draft but no diff to open it in. */
  draftPaths?: readonly string[];
}) {
  if (threads.length === 0 && draftPaths.length === 0) return null;
  return (
    <div className="p-3">
      <p className="text-ui font-medium">Earlier conversations and unavailable diffs</p>
      {draftPaths.map((path) => (
        <div key={`draft:${path}`}>
          <p className="mt-3 break-all text-xs text-muted-foreground">{path}</p>
          <FileNewConversation path={path} context={context} />
        </div>
      ))}
      {threads.map((thread) => (
        <div key={thread.id}>
          <p className="mt-3 break-all text-xs text-muted-foreground">{thread.path}</p>
          <PullRequestReviewThread thread={thread} context={context} showContext />
        </div>
      ))}
    </div>
  );
}

const NO_ANNOTATIONS: PullRequestReviewAnnotations = new Map();

export interface PullRequestChangesReviewProps {
  files: readonly PullRequestChangedFile[];
  filesByPath: ReadonlyMap<string, FileDiffMetadata>;
  /** Where the patch `filesByPath` comes from is; files show while it's read, and say if it failed. */
  patch?: PullRequestPatchState;
  /** Reads the patch again, from any file that says it failed. */
  onRetryPatch?: () => void;
  /** Conversations by the changed file they belong to. */
  threadsByFile: ReadonlyMap<string, readonly ReviewThread[]>;
  /** Conversations on no changed file. */
  elsewhereThreads: readonly ReviewThread[];
  context: ReviewThreadContext;
  /**
   * Where its files' code host links open, apart from the conversations' revision: the
   * commit the diff on screen was read at, and the repository holding it.
   */
  fileRevision?: { sha: string | null | undefined; headRepositoryUrl?: string | null };
  /** Whether the conversations were read at the revision the diff shows. */
  matchingRevision: boolean;
  /** The diff's base and head; a selection made at another revision is dropped. */
  revision: string;
  layout: "unified" | "split";
  onLayoutChange: (layout: "unified" | "split") => void;
  /** Loads a file's full text when the reader expands unchanged lines. */
  loadDiffFiles?: FileDiffContentsLoader;
  /** The conversation to bring into view and focus; each new object is one request. */
  focusedThread: { id: string } | null;
  unresolvedCount: number;
  onNavigateUnresolved: (direction: 1 | -1) => void;
  loadingThreads: boolean;
  /** The files and patch are the previous push's, shown while the latest push's are read. */
  outdated?: boolean;
  /** False while the panel is mounted but hidden; its ⌘K commands are withdrawn. */
  active?: boolean;
  /** A file a tab's place names; the review scrolls to it whenever it changes. */
  revealPath?: string | null;
  /** Opens a changed file in its own tab of this pull request, where tabs exist. */
  onOpenFileInNewTab?: (path: string, options: { background: boolean }) => void;
  /** A file the reader picked in the changed-files tree, for a tab that names it. */
  onFileChosen?: (path: string) => void;
  /** The changed files the reader owns, marked in place; null marks none. */
  ownedPaths?: ReadonlySet<string> | null;
  /** What the changes compare with, such as a stacked PR's choice, beside the file count. */
  base?: DiffReviewBase | null;
}

/** Where the reader was in each PR's changes this window, for its Changes to open there again. */
const scrollAnchors = new Map<string, ReviewScrollAnchor>();

export function PullRequestChangesReview({
  files,
  filesByPath,
  patch,
  onRetryPatch,
  threadsByFile,
  elsewhereThreads,
  context,
  fileRevision,
  matchingRevision,
  revision,
  layout,
  onLayoutChange,
  loadDiffFiles,
  focusedThread,
  unresolvedCount,
  onNavigateUnresolved,
  loadingThreads,
  outdated = false,
  active = true,
  revealPath,
  onOpenFileInNewTab,
  onFileChosen,
  ownedPaths = null,
  base = null,
}: PullRequestChangesReviewProps) {
  // The review lists them in the tree's order, so ownership arriving later moves nothing.
  const reviewFiles = useMemo(() => {
    const listed = files.map((file) => pullRequestReviewFile(file, filesByPath.get(file.path)));
    if (!ownedPaths) return listed;
    return listed.map((file) => ({ ...file, ownedByViewer: ownedPaths.has(file.path) }));
  }, [files, filesByPath, ownedPaths]);
  const contents = useMemo(
    () => pullRequestReviewContents(files, filesByPath, patch),
    [files, filesByPath, patch],
  );
  const retryPatch = useCallback(() => onRetryPatch?.(), [onRetryPatch]);
  const reviewScope = `pull-request:${context.prUrl ?? context.conversationId}`;
  // A file the address names wins over where the reader was.
  const [initialScrollAnchor] = useState(() =>
    revealPath ? undefined : scrollAnchors.get(reviewScope),
  );
  const latestReviewScope = useRef(reviewScope);
  latestReviewScope.current = reviewScope;
  const saveScrollAnchor = useCallback((anchor: ReviewScrollAnchor) => {
    scrollAnchors.set(latestReviewScope.current, anchor);
  }, []);
  const [wrapLines, changeWrapLines] = useSavedWrapLines();
  const [findOpen, setFindOpen] = useState(false);
  const [findFocusRequest, setFindFocusRequest] = useState(0);
  const openFind = useCallback(() => {
    setFindOpen(true);
    setFindFocusRequest((request) => request + 1);
  }, []);

  const lineSelection = useReviewLineSelection(revision);
  const { committed: committedSelection, shown: shownSelection, onLineSelection } = lineSelection;
  const [selectionErrors, setSelectionErrors] = useState<ReadonlyMap<string, string>>(
    () => new Map(),
  );
  const setSelectionError = useCallback((path: string, message: string | undefined) => {
    setSelectionErrors((current) => {
      if (current.get(path) === message) return current;
      const next = new Map(current);
      if (message) next.set(path, message);
      else next.delete(path);
      return next;
    });
  }, []);

  const draftEntries = useNewReviewThreadDrafts(context.conversationId, context.prUrl);
  const drafts = useMemo(() => {
    const placements = new Map<string, ReviewDraftPlacement>();
    for (const [path, draft] of draftEntries) {
      if (!draft.anchor) continue;
      const stale =
        draft.anchor.head_sha !== context.headSha || draft.anchor.base_sha !== context.baseSha;
      placements.set(path, { anchor: draft.anchor, stale });
    }
    return placements;
  }, [draftEntries, context.headSha, context.baseSha]);

  const previousAnnotations = useRef<PullRequestReviewAnnotations>(NO_ANNOTATIONS);
  const { annotations, withoutDiff, draftsWithoutDiff } = useMemo(() => {
    const built = pullRequestReviewAnnotations({
      threadsByFile,
      contents,
      matchingRevision,
      drafts,
      selectionErrors,
    });
    return {
      annotations: keepUnchangedAnnotations(previousAnnotations.current, built.annotations),
      withoutDiff: built.withoutDiff,
      draftsWithoutDiff: built.draftsWithoutDiff,
    };
  }, [threadsByFile, contents, matchingRevision, drafts, selectionErrors]);
  useEffect(() => {
    previousAnnotations.current = annotations;
  }, [annotations]);

  // Next and previous unresolved conversation: reveal its line, then focus it once rendered.
  const shell = useRef<HTMLDivElement | null>(null);
  // ⌘F and the desktop Edit > Find… open the review's find while the reader works in it.
  const isFindActive = useCallback(
    () => shell.current?.contains(document.activeElement) ?? false,
    [],
  );
  useFindSurface({ isActive: isFindActive, openFind });
  const [revealTarget, setRevealTarget] = useState<ReviewRevealTarget | null>(null);
  // Only a new request moves the reader; conversations refreshing don't.
  const latestPlacement = useRef({ annotations, contents });
  latestPlacement.current = { annotations, contents };
  // A request made while diffs are loading waits for its conversation's place, until the reader
  // does something else or every diff has settled without one. Giving up cancels the whole
  // request, so its focus doesn't land later either; a newer request gets its own number.
  const awaitingPlacement = useRef<string | null>(null);
  const request = useRef(0);
  const cancelRequest = useCallback(() => {
    awaitingPlacement.current = null;
    request.current += 1;
  }, []);
  const revealThread = useCallback((id: string) => {
    const { annotations: placed, contents: shown } = latestPlacement.current;
    const location = reviewThreadLocation(placed, shown, id);
    if (!location) return false;
    setRevealTarget({
      path: location.path,
      line: location.line,
      side: location.side,
      align: "center",
    });
    return true;
  }, []);
  useEffect(() => {
    const waiting = awaitingPlacement.current;
    if (waiting === null || revealThread(waiting)) {
      awaitingPlacement.current = null;
      return;
    }
    if (![...contents.values()].some((content) => content.status === "loading")) cancelRequest();
  }, [annotations, contents, revealThread, cancelRequest]);
  useEffect(() => {
    const container = shell.current;
    if (!container) return;
    const events = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const event of events) {
      container.addEventListener(event, cancelRequest, { passive: true, capture: true });
    }
    return () => {
      for (const event of events) {
        container.removeEventListener(event, cancelRequest, { capture: true });
      }
    };
  }, [cancelRequest]);
  useEffect(() => {
    if (!focusedThread) return;
    const current = ++request.current;
    const loading = [...latestPlacement.current.contents.values()].some(
      (content) => content.status === "loading",
    );
    awaitingPlacement.current =
      revealThread(focusedThread.id) || !loading ? null : focusedThread.id;
    const container = shell.current;
    if (!container) return;
    const focusThread = () => {
      if (request.current !== current) return true;
      const element = container.querySelector<HTMLElement>(
        `[data-review-thread="${CSS.escape(focusedThread.id)}"]`,
      );
      if (!element || !element.getClientRects().length) return false;
      element.scrollIntoView({ block: "center" });
      element.focus({ preventScroll: true });
      return true;
    };
    if (focusThread()) return;
    const observer = new MutationObserver(() => {
      if (focusThread()) observer.disconnect();
    });
    observer.observe(container, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [focusedThread, revealThread]);

  const fileDiffFor = useCallback(
    (path: string) => {
      const content = contents.get(path);
      return content?.status === "ready" ? content.fileDiff : undefined;
    },
    [contents],
  );

  const renderAnnotation = useCallback(
    (annotation: DiffLineAnnotation<PullRequestReviewAnnotation>, path: string) => (
      <ReviewAnnotationRow
        annotation={annotation.metadata}
        path={path}
        context={context}
        fileDiff={matchingRevision ? fileDiffFor(path) : undefined}
      />
    ),
    [context, matchingRevision, fileDiffFor],
  );

  const canCreate = !!context.canCreate;
  const renderGutterUtility = useCallback(
    (hoveredLine: DiffReviewHoveredLine, path: string) => {
      const draft = drafts.get(path);
      const fileDiff = fileDiffFor(path);
      // An open composer on this file's diff takes the place of the "+".
      if ((draft && !draft.stale) || !fileDiff) return null;
      return (
        <GutterConversationButton
          path={path}
          fileDiff={fileDiff}
          context={context}
          hoveredLine={hoveredLine}
          selected={committedSelection?.path === path ? committedSelection.range : null}
          onSelectionError={setSelectionError}
        />
      );
    },
    [drafts, fileDiffFor, context, committedSelection, setSelectionError],
  );

  const renderHeaderExtras = useCallback(
    (file: DiffReviewFile) => {
      // A renamed file's old path is already in its header, before the new one.
      const conversations = threadsByFile.get(file.path)?.length ?? 0;
      if (conversations === 0) return null;
      return (
        <span
          className="flex shrink-0 items-center gap-1 text-muted-foreground"
          title={`${conversations} conversation${conversations === 1 ? "" : "s"}`}
        >
          <MessageSquareIcon className="size-3" aria-hidden="true" />
          {conversations}
        </span>
      );
    },
    [threadsByFile],
  );

  const footerThreads = useMemo(
    () => [...withoutDiff, ...elsewhereThreads],
    [withoutDiff, elsewhereThreads],
  );
  const footer = useMemo(
    () =>
      footerThreads.length > 0 || draftsWithoutDiff.length > 0 ? (
        <EarlierConversations
          threads={footerThreads}
          draftPaths={draftsWithoutDiff}
          context={context}
        />
      ) : undefined,
    [footerThreads, draftsWithoutDiff, context],
  );

  const toolbarStatus = outdated ? (
    <>
      <DiffReviewToolbarProgress label="Loading the latest push" active />
      <Badge variant="outline" title="These are the previous push's changes.">
        Outdated
      </Badge>
    </>
  ) : (
    <DiffReviewToolbarProgress label="Loading review threads" active={loadingThreads} />
  );
  const toolbar: ReactNode = (
    <>
      {unresolvedCount > 0 && (
        <>
          <IconButton
            label="Previous unresolved conversation"
            onClick={() => onNavigateUnresolved(-1)}
            className="size-7 text-muted-foreground"
          >
            <ChevronUpIcon className="size-4" />
          </IconButton>
          <IconButton
            label="Next unresolved conversation"
            onClick={() => onNavigateUnresolved(1)}
            className="size-7 text-muted-foreground"
          >
            <ChevronDownIcon className="size-4" />
          </IconButton>
        </>
      )}
      <IconButton
        label={FIND_IN_CHANGES_LABEL}
        onClick={openFind}
        className="size-7 text-muted-foreground"
      >
        <SearchIcon className="size-4" />
      </IconButton>
    </>
  );

  const expandContext = useMemo(
    () => (loadDiffFiles ? { load: loadDiffFiles, lineCount: 10 } : undefined),
    [loadDiffFiles],
  );
  const selectedLines = useMemo(
    () => (shownSelection ? { path: shownSelection.path, range: shownSelection.range } : null),
    [shownSelection],
  );
  // Keyed by the PR's facts, not the context object, which the panel rebuilds every render.
  const { conversationId, prUrl, headSha, provider } = context;
  const fileSha = fileRevision ? fileRevision.sha : headSha;
  const hasHeadRepository = !!fileRevision && "headRepositoryUrl" in fileRevision;
  const headRepositoryUrl = fileRevision?.headRepositoryUrl;
  const fileActions = useCallback(
    (file: DiffReviewFile) =>
      pullRequestFileActions(
        file,
        {
          conversationId,
          prUrl,
          provider,
          sha: fileSha,
          ...(hasHeadRepository ? { headRepositoryUrl } : {}),
        },
        committedSelection,
      ),
    [
      conversationId,
      prUrl,
      fileSha,
      hasHeadRepository,
      headRepositoryUrl,
      provider,
      committedSelection,
    ],
  );
  const folderActions = useCallback(
    (path: string) => pullRequestFolderActions(path, conversationId),
    [conversationId],
  );
  const source: DiffReviewSourceLayer<PullRequestReviewAnnotation> = {
    // Upstream's names for a pull request's find, kept apart from the session review's.
    findHighlightNames: PULL_REQUEST_FIND_HIGHLIGHTS,
    annotations,
    renderAnnotation,
    // Anyone can pick lines, for links to them; only the "+" that starts a conversation needs access.
    ...(canCreate && { renderGutterUtility }),
    onLineSelection,
    selectedLines,
    expandContext,
    renderHeaderExtras,
    toolbar,
    toolbarStatus,
    footer,
  };

  return (
    <div
      ref={shell}
      className="min-h-0 flex-1"
      // A canceled pointer gesture goes back to the committed selection.
      onPointerCancel={lineSelection.cancelPreview}
    >
      <DiffReview<PullRequestReviewAnnotation>
        diagnosticsView="pull_request"
        files={reviewFiles}
        contents={contents}
        onRetry={retryPatch}
        layout={layout}
        onLayoutChange={onLayoutChange}
        wrapLines={wrapLines}
        onWrapLinesChange={changeWrapLines}
        commandHeading="Pull request changes"
        commandsActive={active}
        findOpen={findOpen}
        findFocusRequest={findFocusRequest}
        onFindClose={() => setFindOpen(false)}
        emptyMessage="No changes vs base."
        revealTarget={revealTarget}
        revealPath={revealPath}
        source={source}
        fileActions={fileActions}
        folderActions={folderActions}
        onOpenFileInNewTab={onOpenFileInNewTab}
        onTreeFileChosen={onFileChosen}
        presentation={PRESENTATION}
        reviewScope={reviewScope}
        initialScrollAnchor={initialScrollAnchor}
        onScrollAnchorChange={saveScrollAnchor}
        base={base}
      />
    </div>
  );
}

/** One row under a diff line: its conversations, and a new one being written there. */
function ReviewAnnotationRow({
  annotation,
  path,
  context,
  fileDiff,
}: {
  annotation: PullRequestReviewAnnotation;
  path: string;
  context: ReviewThreadContext;
  fileDiff: FileDiffMetadata | undefined;
}) {
  return (
    <div className="min-w-0 px-3 font-sans [white-space:normal]">
      {annotation.selectionError && (
        <p role="alert" className="py-2 text-ui">
          {annotation.selectionError}
        </p>
      )}
      {annotation.newThread && <FileNewConversation path={path} context={context} />}
      {annotation.threads.map((thread) => (
        <PullRequestReviewThread
          key={thread.id}
          thread={thread}
          context={context}
          fileDiff={fileDiff}
          showContext={annotation.fileLevel}
        />
      ))}
    </div>
  );
}

function useFileComposer(path: string, context: ReviewThreadContext) {
  return usePullRequestReviewThreadComposer(
    context.conversationId,
    context.prUrl,
    path,
    context.headSha ?? "",
    context.baseSha ?? "",
  );
}

function FileNewConversation({ path, context }: { path: string; context: ReviewThreadContext }) {
  const composer = useFileComposer(path, context);
  return <NewConversationComposer composer={composer} context={context} />;
}

function GutterConversationButton({
  path,
  fileDiff,
  context,
  hoveredLine,
  selected,
  onSelectionError,
}: {
  path: string;
  fileDiff: FileDiffMetadata;
  context: ReviewThreadContext;
  hoveredLine: DiffReviewHoveredLine;
  selected: SelectedLineRange | null;
  onSelectionError: (path: string, message: string | undefined) => void;
}) {
  const composer = useFileComposer(path, context);
  return (
    <NewConversationButton
      disabled={composer.draft.pending}
      onClick={() => {
        const range = conversationRange(hoveredLine(), selected);
        if (!range || composer.draft.pending) return;
        const lines = reviewSelection(range, fileDiff);
        if (!lines) {
          onSelectionError(path, SELECTION_OUTSIDE_ONE_HUNK);
          return;
        }
        onSelectionError(path, undefined);
        composer.selectLines(lines);
      }}
    />
  );
}

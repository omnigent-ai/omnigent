// Changes review: the session's working-copy changes as one review over the
// whole changeset, in the reference design. A pane header says what is compared:
// the repository, when its changes were read and the compared revisions, in every
// state, so they stay reachable while a comparison loads or fails. The toolbar
// below it is how the diff shows. The review is the shared one, on the same source.

import { randomUUID } from "@/lib/randomUUID";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  FileDiffIcon,
  FolderGit2Icon,
  GitCommitHorizontalIcon,
  ListTreeIcon,
  Loader2Icon,
  RefreshCwIcon,
} from "lucide-react";

import type { DiffReviewFile } from "@/components/diff-review/diffReviewModel";
import {
  DiffReviewToolbar,
  type DiffReviewToolbarProps,
} from "@/components/diff-review/DiffReviewToolbar";
import type { DiffReviewPresentation } from "@/components/diff-review/reviewPresentation";
import { CHANGES_REVIEW_TREE_WIDTH } from "@/components/diff-review/reviewTreeWidth";
import { viewFileAction } from "@/components/diff-review/viewFileAction";
import { SandboxResumeNotice } from "@/components/SandboxResumeNotice";
import {
  FoldingSectionTabs,
  focusSectionControl,
  type SectionTab,
} from "@/components/sectionTabs/SectionTabs";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { useElementWidth } from "@/hooks/useElementWidth";
import type { BranchCommitsView } from "@/lib/branchCommits";
import { cn } from "@/lib/utils";
import { WORKSPACE_TOOLBAR_CLASS } from "./workspaceTabs/WorkspaceSidebarLayout";
import { useWorkspaceAsleep } from "@/hooks/useWorkspaceChangedFiles";
import { useForgetDiffTextWhenRevoked, useForgetWhenRevoked } from "@/hooks/useWorkingCopyChanges";
import { BRANCH_COMMITS_KEY } from "@/lib/branchCommits";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { useFindSurface } from "@/lib/findRouting";
import { type LocalComparisonView, onComparisonChosen } from "@/lib/localComparison";
import {
  comparisonHeadIsWorkingCopy,
  type EffectiveComparison,
  hostListing,
} from "@/lib/workingCopyComparison";
import {
  AVAILABLE,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import { ComparisonPicker } from "./ChangesReviewComparison";
import { BranchCommitList, SHOW_A_COMMIT_LABEL } from "./ChangesReviewCommits";
import { type CommitStepFocus, OpenBranchCommit } from "./ChangesReviewCommit";
import { BRANCH_COMMIT_KEYWORDS } from "./pullRequestCommits/commitCommands";
import { useCommitCommands } from "./pullRequestCommits/useCommitCommands";
import { useBranchCommits } from "./useBranchCommits";
import { REPOSITORY_REFS_KEY, useRepositoryRefs } from "./useRepositoryRefs";
import { reviewFileActions, reviewFolderActions } from "./fileCommands/reviewFileActions";
import { resolveFileCommandTarget, useSessionHostId } from "./fileCommands/target";
import { useFileCommandEnvironment } from "./fileCommands/useFileCommandFacts";
import { type FilePosition, useWorkspacePaths } from "./FileViewerContext";
import { useStopFilePositionOnInput } from "./useStopFilePositionOnInput";
import {
  changedReviewFiles,
  publishShownReview,
  type ShownReviewFiles,
} from "@/lib/reviewFileLinks";
import type { ChangedSort } from "@/lib/changedSort";
import type { ComparisonChoice } from "@/lib/comparisonChoice";
import { useLocalComparison } from "./useLocalComparison";
import { useVisitComparison, type VisitComparisonOptions } from "./useVisitComparison";
import type { CommentSelectionRequest } from "./commentSelectionHandoff";
import { actionsOnlyCommentContext } from "./useReviewCommentLayer";
import {
  folderName,
  type ReviewFolder,
  ReviewFolderName,
  useReviewFolder,
} from "./ChangesReviewFolder";
import { ComparisonOwnerContext, useSessionChoiceCount } from "@/hooks/useComparisonOwner";
import { repositoryPathOf, workspacePathInRepository } from "@/lib/repositoryFolder";
import {
  browseLocationChanges,
  rememberBrowseLocation,
  useBrowseLocationChanges,
} from "./filesPane/browseLocations";
import { sessionChoiceCount, workingCopyComparisonsAt } from "@/lib/workingCopyComparisons";
import { unrepresentableNote, WorkingCopyDiffReviewBody } from "./WorkingCopyDiffReview";
import { useLastInteractedSurface } from "./useLastInteractedSurface";
import {
  landingStands,
  sameReviewFolder,
  type VisitLanding,
  visitFolderStep,
} from "./visitFolderRestore";

const REVIEW_METADATA_KEYS = [BRANCH_COMMITS_KEY, REPOSITORY_REFS_KEY] as const;
/** A commit's review toolbar: how its diff shows, with nothing about the session's comparison. */
function renderCommitToolbar(toolbar: DiffReviewToolbarProps) {
  return <DiffReviewToolbar {...toolbar} />;
}
const NO_ACTIONS: readonly { label: string; run: () => void; primary?: boolean }[] = [];

const NO_VISIT: VisitComparisonOptions["onShown"] = () => {};

const SAVED_BASE_NAMES = {
  automatic: "Automatic",
  head: "Uncommitted changes",
  "branch-point": "All branch changes",
} as const;

/** A comparison in plain words, as the menu names it. */
function comparisonChoiceName(choice: ComparisonChoice): string {
  if (choice.kind === "saved") return SAVED_BASE_NAMES[choice.intent];
  return `“${choice.base}” with “${choice.head || "Working Copy"}”`;
}

export interface ChangesReviewProps {
  conversationId: string;
  /** The file to show; the review scrolls to it whenever it changes. */
  revealPath?: string;
  /** A cited line in `revealPath` to bring into view; each new object is one request. */
  revealPosition?: FilePosition;
  /** A base and head a link asked for; compared as the session's own where typed ones can be. */
  requestedRange?: { base: string; head: string };
  /** The comparison this visit showed, which showing it again applies to the session. */
  comparison?: ComparisonChoice;
  /** The folder, absolute, whose review showed `comparison`; null for the workspace. */
  comparisonRoot?: string | null;
  /**
   * Records a comparison the session shows on the visit, with the folder whose review shows it
   * (`root`, null for the workspace); without it, visits keep none.
   */
  onComparisonShown?: (
    shown: ComparisonChoice,
    options: { step: boolean; root: string | null },
  ) => void;
  /**
   * How the tab last landed on the visit, which brings back the visit's folder and then applies
   * its comparison, once.
   */
  landing?: VisitLanding;
  /** The tab is selected and the sidebar is open. */
  active: boolean;
  /** Shows a file itself, in a Files tab. */
  onViewFile: (path: string) => void;
  /** Leaves a file the changes don't include, for the whole review. */
  onShowAllChanges: () => void;
  /** Leaves a requested base and head for the session's own comparison. */
  onShowDefaultComparison: () => void;
  /** Starts a comment on a selection in a file's own view; without it, there's no Add comment. */
  onCommentOnFile?: (request: CommentSelectionRequest) => void;
  /** Opens a changed file in a tab of its own, from a middle click or ⌘Enter on its tree row. */
  onOpenFileInNewTab?: (path: string, options: { background: boolean }) => void;
  /** The changed files' order, shared with the Changes list. */
  sort: ChangedSort;
}

/** Layout, whitespace and wrapping, shared with the file viewer's diff. */
function useReviewViewPreferences() {
  const [preferences, setPreferences] = useState(readFileViewPreferences);
  const update = useCallback(
    (change: Partial<Pick<typeof preferences, "diffLayout" | "hideWhitespace" | "wrapLines">>) => {
      setPreferences((current) => ({ ...current, ...change }));
      // Merged into a fresh read, so the file viewer's own choices stay as they are.
      writeFileViewPreferences({ ...readFileViewPreferences(), ...change });
    },
    [],
  );
  return { preferences, update };
}

export const RANGE_PRESET_REASON = "Show the default comparison to choose another base.";

/**
 * The comparison view for a base and head a link asked for and this view can't
 * compare: the trigger names them, marked invalid; Base can't change them; Reset
 * leaves them for the session's own comparison.
 */
export function requestedRangeView(
  view: LocalComparisonView,
  range: { base: string; head: string },
  showDefault: () => void,
): LocalComparisonView {
  return {
    ...view,
    trigger: {
      // A linked range has no plain-word name; the trigger shows its refs.
      name: null,
      base: range.base,
      head: range.head,
      title: `Comparing “${range.base}” with “${range.head}” isn't available here yet.`,
      invalid: true,
    },
    quickChoices: view.quickChoices.map((choice) => ({
      ...choice,
      active: false,
      unavailableReason: RANGE_PRESET_REASON,
      choose: () => {},
    })),
    presets: view.presets.map((preset) => ({
      ...preset,
      selected: false,
      unavailableReason: RANGE_PRESET_REASON,
      choose: () => {},
    })),
    reset: { disabled: false, run: showDefault },
    // Typing here would change the session's comparison, not the linked one.
    revisions: { ...view.revisions, editable: false },
    revisionsNote: view.revisionsNote ?? RANGE_PRESET_REASON,
    fallback: null,
    // Nothing about the session's own comparison applies to the range, such as reconnecting.
    state: {
      kind: "unavailable",
      title: "Comparison Unavailable",
      message: `Comparing “${range.base}” with “${range.head}” isn't available here yet.`,
      canRetry: false,
    },
  };
}

/** The header's note beside Refresh; `prefix` gives way in a narrow header. */
interface HeaderStatus {
  prefix?: string;
  label: string;
  title?: string;
}

/** When the changes were read: the minute in the header, the second on hover. */
function capturedLabel(updatedAt: number): HeaderStatus | null {
  if (!updatedAt) return null;
  const format = (options: Intl.DateTimeFormatOptions) =>
    new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit", ...options }).format(
      updatedAt,
    );
  return {
    prefix: "Updated ",
    label: format({}),
    title: `Updated ${format({ second: "2-digit" })}`,
  };
}

/** The review's two sections: the branch's commits, and the changes compared. */
export type ReviewSection = "commits" | "changes";

/** The sections' tabs; Commits counts the branch's commits once they're listed. */
export function reviewSectionTabs(commits: BranchCommitsView): SectionTab<ReviewSection>[] {
  const listed = commits.kind === "listed" ? commits : null;
  return [
    {
      id: "commits",
      label: "Commits",
      icon: GitCommitHorizontalIcon,
      ...(listed && { count: listed.commits.length, partial: listed.truncated }),
    },
    { id: "changes", label: "Changes", icon: FileDiffIcon },
  ];
}

/** Below this header width the section tabs fold into their picker, as on a phone. */
const SECTIONS_FOLD_PX = 440;

/** The review's sections in the header, where its commits can be listed. */
interface HeaderSections {
  tabs: readonly SectionTab<ReviewSection>[];
  shown: ReviewSection;
  select: (section: ReviewSection) => void;
}

/**
 * One row saying what is compared: the repository and its compared revisions,
 * read as one phrase, then the sections, and when they were read beside Refresh.
 */
function PaneHeader({
  repository,
  status,
  comparison,
  sections,
  sectionsRef,
  onRefresh,
}: {
  /** The repository's name, or the button that chooses another. */
  repository: React.ReactNode;
  status: HeaderStatus | null;
  comparison: React.ReactNode;
  /** Commits and Changes, where the branch's commits can be listed; none elsewhere. */
  sections: HeaderSections | null;
  /** Where the sections' tab or picker is, for moving focus to it. */
  sectionsRef: React.RefObject<HTMLDivElement | null>;
  onRefresh: () => void;
}) {
  const headerRef = useRef<HTMLDivElement>(null);
  const width = useElementWidth(headerRef);
  return (
    <div
      ref={headerRef}
      className={cn(WORKSPACE_TOOLBAR_CLASS, "gap-1.5 pl-3")}
      data-testid="changes-review-header"
    >
      <FolderGit2Icon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
      {repository}
      {/* On a phone the comparison's name truncates too, so the tabs and Refresh stay on screen. */}
      <div className="flex min-w-12 shrink items-center">{comparison}</div>
      <span className="ml-auto flex shrink-0 items-center gap-0.5 pl-2">
        {sections && (
          <div ref={sectionsRef} className="contents">
            <FoldingSectionTabs
              tabs={sections.tabs}
              shown={sections.shown}
              select={sections.select}
              folded={width > 0 && width < SECTIONS_FOLD_PX}
              narrow="drop-labels"
              label="Diffs"
              pickerLabel="Diffs section"
              className="mr-1 flex items-center"
            />
          </div>
        )}
        {status && (
          <span
            className={cn(
              "text-xs whitespace-nowrap text-muted-foreground tabular-nums",
              // Refresh's tooltip keeps the time where the header has no room for it.
              sections
                ? "@max-[480px]/workspace-header:hidden"
                : "@max-[360px]/workspace-header:hidden",
            )}
            aria-live="polite"
            title={status.title}
          >
            {/* A narrow header keeps the time and drops the word. */}
            <span className="@max-[560px]/workspace-header:hidden">{status.prefix}</span>
            {status.label}
          </span>
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground"
          aria-label="Refresh Diffs"
          title={status?.title ? `Refresh Diffs (${status.title})` : "Refresh Diffs"}
          onClick={onRefresh}
        >
          <RefreshCwIcon />
        </Button>
      </span>
    </div>
  );
}

/**
 * A row under the toolbar while the changes shown aren't current or aren't the
 * comparison asked for, as the classic review's status row shows them. A note
 * that only says why Automatic chose the last commit stays in the popover.
 */
function ComparisonStatusRow({ view, asleep }: { view: LocalComparisonView; asleep: boolean }) {
  const ready = view.state.kind === "ready" ? view.state.status : null;
  // An asleep sandbox isn't coming back on its own; its notice says so instead.
  const status = asleep && ready?.kind === "reconnecting" ? null : ready;
  const fallback = view.fallback?.override || view.fallback?.canRetry ? view.fallback : null;
  if (!status && !fallback) return null;
  // A typed comparison never falls back to the last commit.
  const branchPoint = status && view.trigger.base !== "HEAD" && !view.revisions.custom;
  return (
    <div
      role="status"
      className="flex shrink-0 items-center gap-2 border-b border-border bg-muted/40 px-3 py-1 text-xs text-muted-foreground"
      data-testid="changes-review-status"
    >
      {status?.kind === "reconnecting" && (
        <Loader2Icon aria-hidden="true" className="size-3 shrink-0 animate-spin" />
      )}
      <span className="min-w-0 flex-1 truncate">{status?.message ?? fallback?.message}</span>
      {status?.kind === "stale" && (
        <Button type="button" variant="ghost" size="xs" onClick={view.retry}>
          Try Again
        </Button>
      )}
      {branchPoint && (
        <Button type="button" variant="ghost" size="xs" onClick={view.showLastCommit}>
          Show Uncommitted Changes
        </Button>
      )}
      {!status && fallback?.override && (
        <Button type="button" variant="ghost" size="xs" onClick={view.compareAgain}>
          Compare Again
        </Button>
      )}
      {!status && fallback && !fallback.override && (
        <Button type="button" variant="ghost" size="xs" onClick={fallback.retry}>
          Try Again
        </Button>
      )}
    </div>
  );
}

/** Under the toolbar: the comparison's status, and how many files the listing left out. */
function ComparisonNotes({
  view,
  comparison,
  asleep,
}: {
  view: LocalComparisonView;
  comparison: EffectiveComparison;
  asleep: boolean;
}) {
  const listing = hostListing(comparison);
  const leftOut = listing ? unrepresentableNote(listing.unrepresentableNames) : null;
  return (
    <>
      <ComparisonStatusRow view={view} asleep={asleep} />
      {leftOut && (
        <p
          className="shrink-0 border-b border-border px-3 py-1 text-xs text-muted-foreground"
          data-testid="changes-review-left-out"
        >
          {leftOut}
        </p>
      )}
    </>
  );
}

/** The toolbar while no review is shown: the tree icon and any file count. */
function ToolbarSkeleton({ fileCount }: { fileCount: number | null }) {
  return (
    <div
      role="toolbar"
      aria-label="Diff review"
      className="flex min-h-9 shrink-0 items-center gap-x-1 border-b border-border px-2 py-0.5 text-ui"
    >
      <span
        className="flex size-7 items-center justify-center text-muted-foreground"
        aria-hidden="true"
      >
        <ListTreeIcon className="size-4" />
      </span>
      {fileCount !== null && (
        <span className="text-muted-foreground" data-testid="diff-review-file-count">
          {fileCount} {fileCount === 1 ? "file" : "files"}
        </span>
      )}
    </div>
  );
}

function ReviewMessage({
  title,
  message,
  alert = false,
  busy = false,
  actions = NO_ACTIONS,
}: {
  title: string;
  message: string;
  alert?: boolean;
  busy?: boolean;
  actions?: readonly { label: string; run: () => void; primary?: boolean }[];
}) {
  return (
    <div
      role={alert ? "alert" : "status"}
      className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-ui"
      data-testid="changes-review-message"
    >
      <strong className="font-semibold">{title}</strong>
      <span className="flex items-center gap-2 text-muted-foreground">
        {busy && <Loader2Icon aria-hidden="true" className="size-4 animate-spin" />}
        {message}
      </span>
      {actions.length > 0 && (
        <div className="mt-1 flex flex-wrap justify-center gap-2">
          {actions.map((action) => (
            <Button
              key={action.label}
              type="button"
              size="sm"
              variant={action.primary ? "default" : "outline"}
              onClick={action.run}
            >
              {action.label}
            </Button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * The session's Diffs: the repository holding the directory Files shows, such as a
 * worktree inside the workspace, reviewed apart from the workspace's own comparison.
 */
export function ChangesReview(props: ChangesReviewProps) {
  const { conversationId, active, comparison, comparisonRoot, onComparisonShown, landing } = props;
  // A landing stands until a comparison or a folder is chosen; then the review shows the choice.
  const choices = useSessionChoiceCount(conversationId);
  const folders = useBrowseLocationChanges(conversationId);
  const stands = landingStands(landing, { choices, folders });
  const visitRoot = comparisonRoot ?? null;
  const folder = useReviewFolder(
    conversationId,
    active,
    stands && comparison !== undefined ? visitRoot : null,
  );
  const { repository, workspace, workspaceKey } = folder;
  // A visit's comparison is its folder's. Another folder's review never applies it, and records
  // its own comparison on the visit in place, with its folder.
  const shownRoot = repository ? folder.root : null;
  const sameFolder = sameReviewFolder(visitRoot, shownRoot);
  const folderStep = visitFolderStep({
    stands,
    active,
    hasComparison: comparison !== undefined,
    visitRoot,
    shownRoot,
    directory: folder.directory,
    resolved: folder.resolved,
    visitRootShows: sameReviewFolder(folder.rootFor(visitRoot), visitRoot),
    canChoose: folder.canChoose,
  });
  // Before paint, move Diffs and Files to the visit's folder; the landing then stands there.
  const restoreTo = folderStep.kind === "restore" ? folderStep.directory : undefined;
  useLayoutEffect(() => {
    if (restoreTo === undefined || !landing) return;
    const now = {
      choices: sessionChoiceCount(conversationId),
      folders: browseLocationChanges(conversationId),
    };
    if (!landingStands(landing, now)) return;
    rememberBrowseLocation(conversationId, restoreTo);
    landing.folders = browseLocationChanges(conversationId);
  }, [restoreTo, landing, conversationId]);
  // While the folder is on its way or unknown, the review neither applies, records nor waits on
  // the visit, so it never records a comparison under the wrong folder.
  const holding = folderStep.kind !== "none" || !folder.resolved;
  const visit = useMemo((): Pick<
    ChangesReviewAtProps,
    "comparison" | "onComparisonShown" | "landing"
  > => {
    if (holding) return { comparison: undefined, onComparisonShown: undefined, landing: undefined };
    const recordHere: VisitComparisonOptions["onShown"] | undefined =
      onComparisonShown &&
      ((shown, { step }) => onComparisonShown(shown, { step, root: shownRoot }));
    return {
      comparison: sameFolder ? comparison : undefined,
      onComparisonShown: recordHere,
      landing: stands ? landing : undefined,
    };
  }, [holding, sameFolder, onComparisonShown, comparison, shownRoot, stands, landing]);
  // A repository's review names files as the session does; kept across renders, so the
  // review's file actions and comments don't rebuild when the parent renders.
  const { onViewFile, onCommentOnFile, onOpenFileInNewTab, revealPath } = props;
  const repositoryActions = useMemo((): Pick<
    ChangesReviewAtProps,
    "onViewFile" | "onCommentOnFile" | "onOpenFileInNewTab"
  > | null => {
    if (!repository) return null;
    const toSession = (path: string) => workspacePathInRepository(path, repository);
    return {
      onViewFile: (path) => onViewFile(toSession(path)),
      onCommentOnFile:
        onCommentOnFile &&
        ((request) => onCommentOnFile({ ...request, path: toSession(request.path) })),
      onOpenFileInNewTab:
        onOpenFileInNewTab && ((path, options) => onOpenFileInNewTab(toSession(path), options)),
    };
  }, [repository, onViewFile, onCommentOnFile, onOpenFileInNewTab]);
  const { onComparisonShown: _recorded, comparisonRoot: _root, ...shared } = props;
  const mapped: ChangesReviewAtProps = repositoryActions
    ? {
        ...shared,
        ...visit,
        ...repositoryActions,
        // A link to a workspace file shows only if it lies in this repository.
        revealPath: revealPath
          ? (repositoryPathOf(revealPath, repository ?? "", workspace) ?? undefined)
          : revealPath,
      }
    : { ...shared, ...visit };
  const folderOwner = useMemo(
    () => (repository ? workingCopyComparisonsAt(repository, workspaceKey) : null),
    [repository, workspaceKey],
  );
  if (repository === null) {
    return (
      <>
        <ReviewMessage
          title="Can't Show This Folder in Diffs"
          message={`${folderName(folder.root)} is outside the workspace. Diffs can show worktrees inside ${folderName(workspace)}.`}
          actions={[
            { label: `Show ${folderName(workspace)}`, run: folder.showWorkspace, primary: true },
            ...(folder.canChoose ? [{ label: "Choose Folder…", run: folder.choose }] : []),
          ]}
        />
        {folder.dialog}
      </>
    );
  }
  if (!repository || !folderOwner) {
    return (
      <>
        <ChangesReviewAt {...mapped} folder={folder} />
        {folder.dialog}
      </>
    );
  }
  return (
    <ComparisonOwnerContext.Provider value={folderOwner}>
      <ChangesReviewAt key={`${workspaceKey}\u0000${repository}`} {...mapped} folder={folder} />
      {folder.dialog}
    </ComparisonOwnerContext.Provider>
  );
}

/** A review's props, with the visit's comparison recorded as the review shows it. */
type ChangesReviewAtProps = Omit<ChangesReviewProps, "onComparisonShown" | "comparisonRoot"> & {
  onComparisonShown?: VisitComparisonOptions["onShown"];
};

function ChangesReviewAt({
  conversationId,
  revealPath,
  revealPosition,
  requestedRange: linkedRange,
  comparison: visitComparison,
  onComparisonShown,
  landing,
  active,
  onViewFile,
  onShowAllChanges,
  onShowDefaultComparison,
  onCommentOnFile,
  onOpenFileInNewTab,
  sort,
  folder,
}: ChangesReviewAtProps & { folder: ReviewFolder }) {
  const { changes, view: sessionView, choice } = useLocalComparison(conversationId, { active });
  // A landing counts the session's choices in every folder's review, as these do.
  const sessionChoices = useSessionChoiceCount(conversationId);
  // Where typed comparisons work, a linked base and head is the visit's comparison instead.
  const comparesTyped = ["available", "checking"].includes(changes.revisionSupport.kind);
  const requestedRange = comparesTyped ? undefined : linkedRange;
  const pendingVisit = useVisitComparison({
    // A linked range the host can't compare keeps its visit until it can.
    active: active && onComparisonShown !== undefined && requestedRange === undefined,
    visit: visitComparison,
    landing,
    session: choice,
    choices: sessionChoices,
    onShown: onComparisonShown ?? NO_VISIT,
  });
  // While on screen, links to the files it shows stay in this review. A requested base and
  // head it can't compare show none, whatever the session's own comparison lists.
  const comparisonBase = changes.comparison.base;
  const changedList = changes.data;
  const listFailed = changes.isError;
  const comparesRequestedRange = requestedRange !== undefined || pendingVisit !== null;
  // How the session names this review's files: as they are, or inside a worktree's folder.
  const repository = folder.repository ?? "";
  const sessionPath = useMemo(
    () => (repository ? (path: string) => workspacePathInRepository(path, repository) : null),
    [repository],
  );
  const reviewFiles = useMemo((): ShownReviewFiles => {
    if (comparesRequestedRange) return { state: "unavailable" };
    const shown = changedReviewFiles({
      comparison: { base: comparisonBase },
      data: changedList,
      isError: listFailed,
    });
    // Links name files as the session does, so membership is published that way.
    return shown.state === "listed" && sessionPath
      ? { state: "listed", paths: shown.paths.map(sessionPath) }
      : shown;
  }, [comparesRequestedRange, comparisonBase, changedList, listFailed, sessionPath]);
  const publisher = useRef({});
  useEffect(() => {
    if (!active) return;
    return publishShownReview(conversationId, publisher.current, reviewFiles);
  }, [active, conversationId, reviewFiles]);
  const sandboxAsleep = useWorkspaceAsleep(conversationId);
  const rangeBase = requestedRange?.base;
  const rangeHead = requestedRange?.head;
  const view = useMemo(
    () =>
      rangeBase === undefined || rangeHead === undefined
        ? sessionView
        : requestedRangeView(
            sessionView,
            { base: rangeBase, head: rangeHead },
            onShowDefaultComparison,
          ),
    [sessionView, rangeBase, rangeHead, onShowDefaultComparison],
  );
  // Commits or Changes: this review's own choice, never in the URL. Where the branch's commits
  // can't be listed, or the reader may no longer see the session's files, there are no tabs.
  const listsCommits = view.revisions.editable && changes.permitted;
  const [chosenSection, setChosenSection] = useState<ReviewSection>("changes");
  const section: ReviewSection = listsCommits ? chosenSection : "changes";
  // A new file, line, range or comparison to show is a request to see the changes, whichever
  // tab was shown, wherever it came from, such as ⌘K.
  const choiceKey = view.choiceKey;
  const reveal = { revealPath, revealPosition, rangeBase, rangeHead, choiceKey };
  const [shownReveal, setShownReveal] = useState(reveal);
  if (
    shownReveal.revealPath !== revealPath ||
    shownReveal.revealPosition !== revealPosition ||
    shownReveal.rangeBase !== rangeBase ||
    shownReveal.rangeHead !== rangeHead ||
    shownReveal.choiceKey !== choiceKey
  ) {
    setShownReveal(reveal);
    setChosenSection("changes");
  }
  const changesActive = active && section === "changes";
  // Add comment and Attach to agent on a selection, while the view is shown and its after side is
  // the working copy, the text comments and line tags refer to; nothing is shown inline.
  const workingCopyHead = comparisonHeadIsWorkingCopy(changes.comparison);
  const comments = useMemo(
    () =>
      changesActive && workingCopyHead && onCommentOnFile
        ? actionsOnlyCommentContext({
            canCommentOn: () => true,
            onAddComment: (path, selection, text) =>
              onCommentOnFile({ id: randomUUID(), path, ...selection, text }),
            ...(sessionPath && { sessionPath }),
          })
        : undefined,
    [changesActive, workingCopyHead, onCommentOnFile, sessionPath],
  );
  // Here, above the review, so access lost while no review is shown still clears its text.
  useForgetDiffTextWhenRevoked(conversationId, changes.permitted);
  // The branch's commits and names are the session's too.
  useForgetWhenRevoked(conversationId, changes.permitted, REVIEW_METADATA_KEYS);
  const { root } = useWorkspacePaths();
  const sessionHostId = useSessionHostId();
  const fileEnvironment = useFileCommandEnvironment(sessionHostId, conversationId);
  const { preferences, update } = useReviewViewPreferences();
  const [pickerOpen, setPickerOpen] = useState(false);
  // Without access the picker goes, with its typed text; it reopens closed.
  if (pickerOpen && !changes.permitted) setPickerOpen(false);
  const listsRevisions = active && pickerOpen && view.revisions.editable && changes.permitted;
  const suggestions = useRepositoryRefs(conversationId, changes.workspace, {
    enabled: listsRevisions,
  });
  // Listed while the review is on screen, so the Commits tab can count them from either tab, and
  // read again on showing Commits once old, or when the session's files may have changed.
  const commits = useBranchCommits(conversationId, changes.workspace, view.revisions.custom, {
    enabled: active && listsCommits,
    shown: section === "commits",
  });
  const sectionTabs = useMemo(() => reviewSectionTabs(commits.view), [commits.view]);
  // The commit last chosen in Commits; it's open there until the reader goes back to the list.
  const [chosenCommit, setChosenCommit] = useState<{ oid: string; open: boolean } | null>(null);
  const [commitFocus, setCommitFocus] = useState<CommitStepFocus | null>(null);
  const listedCommits = commits.view.kind === "listed" ? commits.view.commits : null;
  const openIndex =
    chosenCommit?.open && listedCommits
      ? listedCommits.findIndex((commit) => commit.oid === chosenCommit.oid)
      : -1;
  // A commit the list no longer includes, as after a rebase, leaves the list on screen.
  const openCommit = openIndex >= 0 ? listedCommits![openIndex]! : null;
  const commitActive = active && section === "commits" && openCommit !== null;
  // Where focus goes once a section is chosen by keyboard or command: the commits' filter (or the
  // list while it loads), or the Changes tab. Each request is one move; nothing moves otherwise.
  const [focusRequest, setFocusRequest] = useState<{ to: ReviewSection; id: number } | null>(null);
  const pendingCommitsFocus = useRef(false);
  const sectionsRef = useRef<HTMLDivElement>(null);
  const focusCommits = useCallback((panel: HTMLElement | null) => {
    const list = panel?.querySelector<HTMLElement>("[data-testid='changes-review-commits']");
    const target = list?.querySelector<HTMLElement>("[data-commits-filter]") ?? list;
    if (!target) return false;
    target.focus();
    return true;
  }, []);
  const commitsRef = useRef<HTMLDivElement | null>(null);
  // The panel mounts a commit after its tab is chosen, so a request made then lands as it mounts.
  const attachCommits = useCallback(
    (panel: HTMLDivElement | null) => {
      commitsRef.current = panel;
      if (panel && pendingCommitsFocus.current) pendingCommitsFocus.current = !focusCommits(panel);
    },
    [focusCommits],
  );
  useEffect(() => {
    if (!focusRequest) return;
    if (focusRequest.to === "changes") {
      focusSectionControl(sectionsRef.current);
      return;
    }
    pendingCommitsFocus.current = !focusCommits(commitsRef.current);
  }, [focusRequest, focusCommits]);
  const showSection = useCallback((next: ReviewSection, moveFocus: boolean) => {
    setChosenSection(next);
    if (moveFocus) setFocusRequest((request) => ({ to: next, id: (request?.id ?? 0) + 1 }));
  }, []);
  const showCommitList = useCallback(() => {
    setChosenCommit((chosen) => chosen && { ...chosen, open: false });
    showSection("commits", true);
  }, [showSection]);
  const commitCommands = useCommitCommands({
    conversationId,
    active: commitActive,
    heading: "Changes",
    facts: {
      tabShown: openCommit !== null,
      onCommitsTab: true,
      open: openCommit && {
        sha: openCommit.oid,
        listed: true,
        previousSha: listedCommits![openIndex - 1]?.oid ?? null,
        nextSha: listedCommits![openIndex + 1]?.oid ?? null,
        moreAfterList: commits.view.kind === "listed" && commits.view.truncated,
      },
      keywords: BRANCH_COMMIT_KEYWORDS,
      order: "newest-first",
    },
    handlers: {
      showCommits: showCommitList,
      openCommit: (oid, direction) => {
        setChosenCommit({ oid, open: true });
        setCommitFocus({ target: direction });
      },
      showAllCommits: showCommitList,
      showChanges: () => showSection("changes", true),
    },
  });
  // ⌘K's way to the picker's Commits section, while this review is on screen.
  const commitsNote = view.revisions.editable ? null : view.revisionsNote;
  const showCommitCommands = useMemo(
    (): PaletteCommand[] =>
      active
        ? [
            {
              id: `show-a-commit:${conversationId}`,
              label: SHOW_A_COMMIT_LABEL,
              icon: GitCommitHorizontalIcon,
              keywords: ["commit", "commits", "history", "log", "diff", "changes"],
              availability: commitsNote ? unavailableBecause(commitsNote) : AVAILABLE,
              movesFocus: true,
              run: showCommitList,
            },
          ]
        : [],
    [active, conversationId, commitsNote, showCommitList],
  );
  usePaletteCommands(`changes-review-commits:${conversationId}`, "Changes", showCommitCommands);

  const container = useRef<HTMLDivElement>(null);
  // A citation the reader moves on from isn't replayed when the review mounts again.
  useStopFilePositionOnInput(container, revealPosition);
  const [findOpen, setFindOpen] = useState(false);
  const [findFocusRequest, setFindFocusRequest] = useState(0);
  // ⌘F finds in the review shown, the changes or a commit's, while the reader works in it.
  const reviewActive = changesActive || commitActive;
  const { activeRef: workingInReview } = useLastInteractedSurface(container, reviewActive);
  const isActiveSurface = useCallback(() => workingInReview.current, [workingInReview]);
  useFindSurface(
    reviewActive
      ? {
          owner: `changes-review:${conversationId}`,
          isActive: isActiveSurface,
          openFind: () => {
            setFindOpen(true);
            setFindFocusRequest((request) => request + 1);
          },
        }
      : null,
  );

  const fileActions = useCallback(
    (file: DiffReviewFile) => [
      viewFileAction(file.path, onViewFile),
      ...reviewFileActions(
        resolveFileCommandTarget(file.path, {
          root,
          base: repository,
          conversationId,
          hostId: sessionHostId,
          deleted: file.change === "deleted",
        }),
        fileEnvironment,
        { onOpen: null, activeFile: true },
      ),
    ],
    [onViewFile, root, repository, conversationId, sessionHostId, fileEnvironment],
  );
  const folderActions = useCallback(
    (path: string) =>
      reviewFolderActions(
        resolveFileCommandTarget(path, {
          root,
          base: repository,
          conversationId,
          hostId: sessionHostId,
        }),
        fileEnvironment,
      ),
    [root, repository, conversationId, sessionHostId, fileEnvironment],
  );

  // Any comparison chosen in the picker shows in Changes, even one that fails or is unchanged.
  const pickerView = useMemo(
    () => (listsCommits ? onComparisonChosen(view, () => setChosenSection("changes")) : view),
    [listsCommits, view],
  );
  // It names the session's branches and holds typed revisions, so it shows only with access.
  const comparison = changes.permitted ? (
    <ComparisonPicker
      view={pickerView}
      open={pickerOpen}
      onOpenChange={setPickerOpen}
      suggestions={suggestions}
    />
  ) : null;
  const renderToolbar = useCallback(
    (toolbar: DiffReviewToolbarProps) => (
      <>
        {/* The compared revisions sit in the header above; this row is how the diff shows. */}
        <DiffReviewToolbar {...toolbar} />
        <ComparisonNotes view={view} comparison={changes.comparison} asleep={sandboxAsleep} />
      </>
    ),
    [view, changes.comparison, sandboxAsleep],
  );
  const typedHead = view.revisions.custom?.head ?? "";
  const presentation = useMemo((): DiffReviewPresentation => {
    const base = view.trigger.base;
    return {
      toolbar: renderToolbar,
      tree: { appearance: "reference" },
      treeRowActions: true,
      filterCommands: true,
      fileBadges: "icons",
      codeMetrics: { fontSizePx: 10.5, lineHeightPx: 18 },
      messages: {
        empty: typedHead
          ? {
              title: "No Changes",
              message: `${typedHead} has no changes since it left ${base}.`,
            }
          : {
              title: "Working Copy Is Clean",
              message:
                base === null
                  ? "There are no changes in the working copy."
                  : `There are no changes compared with ${base}.`,
            },
        noSearchMatches: (query) => ({
          title: "No Matching Files",
          message: `No changed files match “${query}”.`,
          actionLabel: "Clear Search",
        }),
        noFilterMatches: {
          title: "No Matching Files",
          message: "Adjust the language or generated-file filters to show changed files.",
          actionLabel: "Clear Filters",
        },
        onlyWhitespace: {
          title: "Only Whitespace Changed",
          message: "No changes remain when whitespace is ignored.",
        },
      },
      treeColumn: CHANGES_REVIEW_TREE_WIDTH,
    };
  }, [view.trigger.base, typedHead, renderToolbar]);
  // A commit's review: the same chrome, without the session comparison's notes.
  const commitPresentation = useMemo(
    (): DiffReviewPresentation => ({
      ...presentation,
      toolbar: renderCommitToolbar,
      messages: {
        ...presentation.messages,
        empty: { title: "No Changes", message: "This commit doesn't change any files." },
      },
    }),
    [presentation],
  );

  const listed = changes.data?.data;
  const fileCount = listed ? listed.length : null;
  const header = (
    <PaneHeader
      repository={<ReviewFolderName folder={folder} />}
      status={changes.isLoading ? { label: "Checking…" } : capturedLabel(changes.dataUpdatedAt)}
      comparison={comparison}
      sections={
        listsCommits
          ? {
              tabs: sectionTabs,
              shown: section,
              select: (next: ReviewSection) => showSection(next, false),
            }
          : null
      }
      sectionsRef={sectionsRef}
      onRefresh={view.refresh}
    />
  );
  const commitsPanel = (
    <div ref={attachCommits} className="flex min-h-0 flex-1 flex-col">
      {openCommit ? (
        <OpenBranchCommit
          conversationId={conversationId}
          commit={openCommit}
          position={{ index: openIndex, count: listedCommits!.length }}
          commands={commitCommands}
          focusRequest={commitFocus}
          renderChanges={(commitChanges) => (
            <WorkingCopyDiffReviewBody
              // Each commit's review starts at its top, not where the previous one was.
              key={openCommit.oid}
              conversationId={conversationId}
              changedFiles={commitChanges}
              base={null}
              presentation={commitPresentation}
              showComparisonNotes={false}
              revealPath=""
              reviewScope={`commit:${conversationId}:${openCommit.oid}`}
              sort={sort}
              layout={preferences.diffLayout}
              onLayoutChange={(diffLayout) => update({ diffLayout })}
              wrapLines={preferences.wrapLines}
              onWrapLinesChange={(wrapLines) => update({ wrapLines })}
              hideWhitespace={preferences.hideWhitespace}
              onHideWhitespaceChange={(hideWhitespace) => update({ hideWhitespace })}
              findOpen={findOpen && commitActive}
              findFocusRequest={findFocusRequest}
              onFindClose={() => setFindOpen(false)}
              fileActions={fileActions}
              folderActions={folderActions}
              fileActionsInPalette={commitActive}
              onOpenFileInNewTab={onOpenFileInNewTab}
              active={commitActive}
            />
          )}
        />
      ) : (
        <BranchCommitList
          view={commits.view}
          retry={commits.retry}
          shown={chosenCommit?.oid ?? null}
          onChoose={(commit) => {
            setChosenCommit({ oid: commit.oid, open: true });
            setCommitFocus({ target: "commit" });
          }}
        />
      )}
    </div>
  );
  const frame = (toolbar: React.ReactNode, body: React.ReactNode) => (
    <div
      ref={container}
      className="@container/workspace-header flex h-full min-h-0 flex-1 flex-col"
      data-testid="changes-review"
    >
      {listsCommits ? (
        <Tabs
          value={section}
          onValueChange={(next) => showSection(next as ReviewSection, false)}
          className="min-h-0 flex-1 gap-0"
        >
          {header}
          {/* Kept mounted behind Commits, so the review keeps its place; only shown is active. */}
          <TabsContent
            value="changes"
            forceMount
            className="flex min-h-0 flex-col data-[state=inactive]:hidden"
          >
            {toolbar}
            {body}
          </TabsContent>
          <TabsContent value="commits" className="flex min-h-0 flex-col">
            {commitsPanel}
          </TabsContent>
        </Tabs>
      ) : (
        <>
          {header}
          {toolbar}
          {body}
        </>
      )}
    </div>
  );
  const skeleton = (
    <>
      <ToolbarSkeleton fileCount={fileCount} />
      <ComparisonNotes view={view} comparison={changes.comparison} asleep={sandboxAsleep} />
    </>
  );

  const showDefault = { label: "Show Default Comparison", run: view.reset.run, primary: true };
  const editComparison = {
    label: "Edit Comparison",
    run: () => {
      showSection("changes", false);
      setPickerOpen(true);
    },
  };
  if (pendingVisit && "waiting" in pendingVisit) {
    // Not the session's files: they aren't the comparison the visit asked for.
    return frame(
      <ToolbarSkeleton fileCount={null} />,
      <ReviewMessage
        busy
        title="Comparing"
        message={`Comparing ${comparisonChoiceName(pendingVisit.waiting)}…`}
      />,
    );
  }
  if (pendingVisit) {
    // The visit's own comparison stays asked for; the session's is a step away.
    const { choice: asked, reason } = pendingVisit.unavailable;
    return frame(
      <ToolbarSkeleton fileCount={null} />,
      <ReviewMessage
        alert
        title="Comparison Unavailable"
        message={`${comparisonChoiceName(asked)} can't be compared here. ${reason}`}
        actions={[
          {
            label: "Show Default Comparison",
            run: () => onComparisonShown?.(choice.shown, { step: true }),
            primary: true,
          },
        ]}
      />,
    );
  }
  if (requestedRange) {
    // The range's own toolbar: no file count or notes from the session's comparison.
    return frame(
      <ToolbarSkeleton fileCount={null} />,
      <ReviewMessage
        alert
        title="Comparison Unavailable"
        message={`Comparing “${requestedRange.base}” with “${requestedRange.head}” isn't available here yet.`}
        actions={[
          { label: "Show Default Comparison", run: onShowDefaultComparison, primary: true },
        ]}
      />,
    );
  }
  const { state } = view;
  if (state.kind === "no-access") {
    return frame(skeleton, <ReviewMessage alert title={state.title} message={state.message} />);
  }
  // Nothing listed can be trusted as current while the sandbox sleeps, so it never reads as clean.
  if (sandboxAsleep && !listed?.length) {
    return frame(
      <>
        {/* No count either: an empty listing from before it slept isn't "0 files". */}
        <ToolbarSkeleton fileCount={null} />
        <ComparisonNotes view={view} comparison={changes.comparison} asleep />
      </>,
      <SandboxResumeNotice sessionId={conversationId} purpose="see what changed" />,
    );
  }
  if (state.kind === "pending") {
    return frame(skeleton, <ReviewMessage busy title="Comparing" message={state.message} />);
  }
  if (state.kind === "unavailable") {
    return frame(
      skeleton,
      <ReviewMessage
        alert
        title={state.title}
        message={state.message}
        actions={[
          showDefault,
          editComparison,
          ...(state.canRetry ? [{ label: "Try Again", run: view.retry }] : []),
        ]}
      />,
    );
  }
  if (changes.isError && !listed) {
    return frame(
      skeleton,
      <ReviewMessage
        alert
        title="Diff Unavailable"
        message={changes.error?.message || "Couldn't load the changed files."}
        actions={[{ label: "Try Again", run: view.retry }]}
      />,
    );
  }
  if (!listed) {
    return frame(skeleton, <ReviewMessage busy title="Loading" message="Loading changes…" />);
  }
  // A list cached before the sandbox stopped stays readable under a banner.
  const resumeBanner = sandboxAsleep && (
    <SandboxResumeNotice sessionId={conversationId} layout="banner" purpose="see what changed" />
  );
  if (revealPath && !listed.some((entry) => entry.path === revealPath)) {
    const name = revealPath.split("/").at(-1) ?? revealPath;
    return frame(
      skeleton,
      <div className="flex min-h-0 flex-1 flex-col">
        {resumeBanner}
        <ReviewMessage
          title={`No Changes to ${name}`}
          message={`“${revealPath}” isn't changed in this comparison.`}
          actions={[
            { label: "Show All Changes", run: onShowAllChanges, primary: true },
            { label: "View File", run: () => onViewFile(revealPath) },
          ]}
        />
      </div>,
    );
  }
  return frame(
    null,
    <div className="flex min-h-0 flex-1 flex-col">
      {resumeBanner}
      <WorkingCopyDiffReviewBody
        conversationId={conversationId}
        changedFiles={changes}
        comments={comments}
        base={null}
        presentation={presentation}
        showComparisonNotes={false}
        revealPath={revealPath ?? ""}
        position={revealPosition}
        sort={sort}
        layout={preferences.diffLayout}
        onLayoutChange={(diffLayout) => update({ diffLayout })}
        wrapLines={preferences.wrapLines}
        onWrapLinesChange={(wrapLines) => update({ wrapLines })}
        hideWhitespace={preferences.hideWhitespace}
        onHideWhitespaceChange={(hideWhitespace) => update({ hideWhitespace })}
        findOpen={findOpen && changesActive}
        findFocusRequest={findFocusRequest}
        onFindClose={() => setFindOpen(false)}
        fileActions={fileActions}
        folderActions={folderActions}
        fileActionsInPalette={changesActive}
        onOpenFileInNewTab={onOpenFileInNewTab}
        active={changesActive}
      />
    </div>,
  );
}

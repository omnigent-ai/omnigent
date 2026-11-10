// The pull request panel's Changes tab: a toolbar, a jump-to-file sidebar tree
// and every changed file's diff stacked in one scroll view.

import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import {
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronUpIcon,
  ChevronsDownUpIcon,
  ChevronsUpDownIcon,
  Columns2Icon,
  FolderIcon,
  ListFilterIcon,
  Loader2Icon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  Rows2Icon,
} from "lucide-react";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { TabsContent } from "@/components/ui/tabs";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { useResizableColumn } from "@/hooks/useResizableColumn";
import type { usePullRequestReviewThreads } from "@/hooks/usePullRequestReviewThreads";
import { RunnerOfflineError } from "@/hooks/useWorkspaceChangedFiles";
import {
  fetchPullRequestFileContents,
  type NormalizedPullRequestInfo,
  type PullRequest,
  type PullRequestChangedFile,
  type PullRequestChangedFilesResult,
  type usePullRequestChangedFiles,
  usePullRequestCommitDiff,
  usePullRequestCommits,
  usePullRequestCommitsUnsupported,
  type usePullRequestDiff,
} from "@/hooks/usePullRequests";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { gitProviderCopy } from "@/lib/gitProviders";
import { hasCommandModifier } from "@/lib/hotkeys";
import { PULL_REQUEST_DIFF_THEME, type ReviewThread } from "@/lib/pullRequestReviewThreads";
import { patchFileHashes, useViewedFiles, type PullRequestIdentity } from "@/lib/pullRequestViewed";
import { cn } from "@/lib/utils";
import {
  DiffStat,
  IconButton,
  PullRequestFileSection,
  STATUS_META,
  type DiffOptions,
  type FindHighlight,
} from "./PullRequestFileSection";
import { PullRequestCommitBar } from "./PullRequestCommitBar";
import { PullRequestCommitsList } from "./PullRequestCommitsList";
import { PullRequestChangesModeSwitch } from "./PullRequestChangesModeSwitch";
import { PullRequestFileActions } from "./PullRequestFileActions";
import { PullRequestFindBar } from "./PullRequestFindBar";
import { PullRequestPathFilter } from "./PullRequestPathFilter";
import { PullRequestReviewThread, type ReviewThreadContext } from "./PullRequestReviewThread";
import { PullRequestViewedProgress, PullRequestViewedToggle } from "./PullRequestViewedControls";
import {
  buildSearchCorpus,
  changedFilesFromPatch,
  filterChangedPaths,
  findMatches,
  firstMatchAtOrBelow,
  MATCH_LIMIT,
  pullRequestFileUrl,
  pullRequestRepoUrl,
  refreshedMatchIndex,
  selectVisibleFiles,
  unquoteGitPath,
  type Match,
} from "./pullRequestReview";
import { useChangesViewState, type FindState } from "./useChangesViewState";
import { useCommitViewState } from "./useCommitViewState";

/** Index of the match a step moves to; with none active, the first or last by direction. */
function stepTarget(activeIndex: number, direction: 1 | -1, length: number): number {
  if (activeIndex >= 0) return (activeIndex + direction + length) % length;
  return direction > 0 ? 0 : length - 1;
}

/** Frames to wait for a section to mount before a pending jump gives up. */
const REVEAL_RETRY_FRAMES = 30;

/** Centered muted message filling the panel — the shared loading/transient shell. */
export function PanelMessage({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-ui text-muted-foreground">
      {children}
    </div>
  );
}

// ── Sidebar file tree ────────────────────────────────────────────────────
// The changed files group into a folder tree. A single-child directory chain
// collapses into one row (VS Code "compact folders"): a lone change under
// omnigent/runner/x.py shows an "omnigent/runner" row, not two nested folders.
// Files are leaves that jump the diff scroll to their section; folders toggle.

interface TreeFileNode {
  type: "file";
  name: string;
  file: PullRequestChangedFile;
}
interface TreeDirNode {
  type: "dir";
  /** Display name, possibly a compacted chain like "a/b/c". */
  name: string;
  /** Full path of the deepest folder in the chain — the collapse key. */
  path: string;
  children: SidebarTree[];
}
type SidebarTree = TreeFileNode | TreeDirNode;

/** Directories before files, each group kept in first-encounter order. */
function dirsFirst(nodes: SidebarTree[]): SidebarTree[] {
  return [...nodes.filter((n) => n.type === "dir"), ...nodes.filter((n) => n.type === "file")];
}

/** Fold single-dir-child chains into one node (children are compacted first,
 *  so one merge per level suffices). */
function compactNode(node: SidebarTree): SidebarTree {
  if (node.type === "file") return node;
  const children = node.children.map(compactNode);
  if (children.length === 1 && children[0].type === "dir") {
    const only = children[0];
    return {
      type: "dir",
      name: `${node.name}/${only.name}`,
      path: only.path,
      children: only.children,
    };
  }
  return { type: "dir", name: node.name, path: node.path, children: dirsFirst(children) };
}

/** Build the compacted folder tree for the changed-files sidebar. */
function buildSidebarTree(files: PullRequestChangedFile[]): SidebarTree[] {
  const root: TreeDirNode = { type: "dir", name: "", path: "", children: [] };
  for (const file of files) {
    const parts = file.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const part = parts[i];
      let dir = node.children.find((c): c is TreeDirNode => c.type === "dir" && c.name === part);
      if (!dir) {
        dir = { type: "dir", name: part, path: parts.slice(0, i + 1).join("/"), children: [] };
        node.children.push(dir);
      }
      node = dir;
    }
    node.children.push({ type: "file", name: parts[parts.length - 1], file });
  }
  return dirsFirst(root.children.map(compactNode));
}

// VS Code–style tree indentation, tuned for the narrow sidebar.
const TREE_INDENT_STEP = 10;
const TREE_BASE_PAD = 8;
const treeIndent = (depth: number) => depth * TREE_INDENT_STEP + TREE_BASE_PAD;

/**
 * One node in the sidebar file tree — a collapsible folder or a file leaf.
 * Names truncate on the LEFT (rtl) so the end of the path — the most specific
 * part — stays visible; the name isn't `flex-1`, so short names still sit
 * beside their icon rather than drifting right.
 */
function SidebarNode({
  node,
  depth,
  activePath,
  onSelectFile,
  collapsedDirs,
  onToggleDir,
  viewedPaths,
}: {
  node: SidebarTree;
  depth: number;
  activePath: string | null;
  onSelectFile: (path: string) => void;
  collapsedDirs: ReadonlySet<string>;
  onToggleDir: (path: string) => void;
  viewedPaths: ReadonlySet<string>;
}) {
  if (node.type === "file") {
    const meta = STATUS_META[node.file.status];
    const StatusIcon = meta.Icon;
    const viewed = viewedPaths.has(node.file.path);
    return (
      <button
        type="button"
        onClick={() => onSelectFile(node.file.path)}
        title={node.file.path}
        style={{ paddingLeft: `${treeIndent(depth)}px` }}
        className={cn(
          "flex w-full items-center gap-1.5 py-1 pr-2 text-left text-ui hover:bg-muted/60",
          node.file.path === activePath && "bg-muted",
        )}
      >
        {/* The chevron column shows a check for viewed files and stays
            empty otherwise, so the status icon lines up under a folder's icon. */}
        <span className="flex size-3.5 shrink-0 items-center justify-center" aria-hidden>
          {viewed && <CheckIcon className="size-3 text-muted-foreground" />}
        </span>
        {viewed && <span className="sr-only">Viewed, </span>}
        <StatusIcon aria-hidden className={cn("size-3.5 shrink-0", meta.className)} />
        <span className={cn("min-w-0 truncate [direction:rtl]", viewed && "text-muted-foreground")}>
          <bdi>{node.name}</bdi>
        </span>
        <span className="ml-auto flex shrink-0 items-center">
          <DiffStat file={node.file} />
        </span>
      </button>
    );
  }

  const open = !collapsedDirs.has(node.path);
  return (
    <>
      <button
        type="button"
        onClick={() => onToggleDir(node.path)}
        aria-expanded={open}
        title={node.path}
        style={{ paddingLeft: `${treeIndent(depth)}px` }}
        className="flex w-full items-center gap-1.5 py-1 pr-2 text-left text-ui hover:bg-muted/60"
      >
        <ChevronRightIcon
          aria-hidden
          className={cn(
            "size-3.5 shrink-0 text-muted-foreground transition-transform",
            open && "rotate-90",
          )}
        />
        <FolderIcon aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 truncate font-medium [direction:rtl]">
          <bdi>{node.name}</bdi>
        </span>
      </button>
      {open &&
        node.children.map((child) => (
          <SidebarNode
            key={child.type === "file" ? child.file.path : child.path}
            node={child}
            depth={depth + 1}
            activePath={activePath}
            onSelectFile={onSelectFile}
            collapsedDirs={collapsedDirs}
            onToggleDir={onToggleDir}
            viewedPaths={viewedPaths}
          />
        ))}
    </>
  );
}

/** The Changes controls row; `navigation` and `progress` are slots for review tools. */
function ChangesToolbar({
  showFileControls,
  sidebarCollapsed,
  onToggleSidebar,
  filterOpen,
  onToggleFilter,
  canNavigateThreads,
  onNavigateThread,
  loadingThreads,
  diffStyle,
  onToggleDiffStyle,
  allCollapsed,
  onToggleAll,
  navigation,
  progress,
  modeSwitch,
}: {
  showFileControls: boolean;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
  /** Whether the inline filter input is open while the sidebar is collapsed. */
  filterOpen: boolean;
  onToggleFilter?: () => void;
  canNavigateThreads: boolean;
  onNavigateThread: (direction: number) => void;
  loadingThreads: boolean;
  diffStyle: "unified" | "split";
  onToggleDiffStyle: () => void;
  allCollapsed: boolean;
  onToggleAll: () => void;
  navigation?: React.ReactNode;
  progress?: React.ReactNode;
  /** The Changes | Commits control, shown only where the provider lists commits. */
  modeSwitch?: React.ReactNode;
}) {
  return (
    // Wraps when the mode switch, file controls and progress together outgrow a narrow rail.
    <div className="flex w-full shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-1 border-b border-border px-2 py-1">
      <div className="flex min-w-0 flex-auto items-center gap-0.5">
        {modeSwitch}
        {showFileControls && (
          <IconButton
            label={sidebarCollapsed ? "Show file list" : "Hide file list"}
            onClick={onToggleSidebar}
            className="size-5 text-muted-foreground"
          >
            {sidebarCollapsed ? (
              <PanelLeftOpenIcon className="size-3.5" />
            ) : (
              <PanelLeftCloseIcon className="size-3.5" />
            )}
          </IconButton>
        )}
        {canNavigateThreads && (
          <>
            <IconButton
              label="Previous unresolved conversation"
              onClick={() => onNavigateThread(-1)}
              className="size-5 text-muted-foreground"
            >
              <ChevronUpIcon className="size-3.5" />
            </IconButton>
            <IconButton
              label="Next unresolved conversation"
              onClick={() => onNavigateThread(1)}
              className="size-5 text-muted-foreground"
            >
              <ChevronDownIcon className="size-3.5" />
            </IconButton>
          </>
        )}
        {onToggleFilter && (
          <IconButton
            label={filterOpen ? "Hide file filter" : "Show file filter"}
            onClick={onToggleFilter}
            className="size-5 text-muted-foreground"
          >
            <ListFilterIcon className="size-3.5" />
          </IconButton>
        )}
        {navigation}
      </div>
      <div className="flex items-center gap-0.5">
        {progress}
        {loadingThreads && (
          <span
            role="status"
            className="mr-1 flex items-center gap-1 whitespace-nowrap text-xs text-muted-foreground"
          >
            Loading review threads
            <Loader2Icon className="size-3 animate-spin" aria-hidden="true" />
          </span>
        )}
        {showFileControls && (
          <>
            <IconButton
              label={diffStyle === "split" ? "Switch to unified view" : "Switch to split view"}
              onClick={onToggleDiffStyle}
              className="size-5 text-muted-foreground"
            >
              {diffStyle === "split" ? (
                <Rows2Icon className="size-3.5" />
              ) : (
                <Columns2Icon className="size-3.5" />
              )}
            </IconButton>
            <IconButton
              label={allCollapsed ? "Expand all diffs" : "Collapse all diffs"}
              onClick={onToggleAll}
              className="size-5 text-muted-foreground"
            >
              {allCollapsed ? (
                <ChevronsUpDownIcon className="size-3.5" />
              ) : (
                <ChevronsDownUpIcon className="size-3.5" />
              )}
            </IconButton>
          </>
        )}
      </div>
    </div>
  );
}

/** The resizable file-list column; `filter` is a slot above the tree. */
function ChangesSidebar({
  width,
  handleProps,
  filter,
  children,
}: {
  width: number;
  handleProps: ReturnType<typeof useResizableColumn>["handleProps"];
  filter?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{ width: `${width}px` }}
      className="relative shrink-0 overflow-y-auto border-r border-border pb-1"
    >
      {/* Drag the right edge to resize the file list. */}
      <div
        {...handleProps}
        aria-label="Resize file list"
        className="absolute inset-y-0 right-0 z-10 w-1 cursor-col-resize transition-colors hover:bg-primary/30 active:bg-primary/50"
      />
      {filter}
      {children}
    </div>
  );
}

/** Why a file's Open action is unavailable, shown as the disabled item's reason. */
function openUnavailableReason(
  providerLabel: string,
  supported: boolean,
  sourceRepoUrl: string | null | undefined,
): string {
  if (!supported) return `Opening files isn’t supported for ${providerLabel}`;
  if (!sourceRepoUrl) return "Source repository unavailable";
  if (!sourceRepoUrl.startsWith("https://")) return "Repository URL isn’t secure";
  return "Revision unknown";
}

const NO_HIDDEN_PATHS: ReadonlySet<string> = new Set();

/** The message a failed read shows; a runner that went offline gets its own wording. */
function readErrorText(error: Error): string {
  return error instanceof RunnerOfflineError ? "Runner offline." : error.message;
}

/** What an empty file list says: an unreadable commit, a commit without changes, or a PR without any. */
function emptyChangesText(commitView: boolean, commitUnavailable: boolean): string {
  if (commitUnavailable) return "This commit's diff can't be read here.";
  if (commitView) return "This commit has no file changes.";
  return "No changes vs base.";
}

const PR_PARSE_ERROR = "Couldn’t read the PR diff. Refresh or open the PR on its provider.";
const COMMIT_PARSE_ERROR = "Couldn’t read this commit’s diff. Refresh or open it on its provider.";

/** A unified patch split into per-file diffs keyed by path, or `parseError` when it can't be parsed. */
function parsePatchByPath(
  patch: string | undefined,
  parseError: string,
): { filesByPath: Map<string, FileDiffMetadata>; parseError: string | undefined } {
  const filesByPath = new Map<string, FileDiffMetadata>();
  if (!patch) return { filesByPath, parseError: undefined };
  try {
    for (const parsed of parsePatchFiles(patch)) {
      for (const file of parsed.files) filesByPath.set(unquoteGitPath(file.name), file);
    }
  } catch {
    return { filesByPath, parseError };
  }
  return { filesByPath, parseError: undefined };
}

/**
 * The Changes tab content. Mounted beside the other tabs' content (it renders its
 * own TabsContent) so collapse and scroll-spy state survive tab switches.
 */
export function PullRequestChangesTab({
  conversationId,
  data,
  pr,
  prUrl,
  canEdit,
  selected,
  changes,
  prDiff,
  reviews,
  identity,
}: {
  conversationId: string;
  data: NormalizedPullRequestInfo;
  pr: PullRequest;
  /** The PR URL the diff queries use. */
  prUrl: string | undefined;
  canEdit: boolean;
  /** The Changes tab is the active tab. */
  selected: boolean;
  changes: ReturnType<typeof usePullRequestChangedFiles>;
  prDiff: ReturnType<typeof usePullRequestDiff>;
  reviews: ReturnType<typeof usePullRequestReviewThreads>;
  /** The PR's server identity, for the viewed-file key; null when unknown. */
  identity: PullRequestIdentity | null;
}) {
  const baseRef = data.base_ref ?? undefined;
  const headSha = pr.head_sha;
  const baseSha = pr.base_sha;
  const mobile = useIsMobileViewport();
  const view = useChangesViewState(conversationId, data.selected_pr_url ?? pr.url);
  const { revealedPaths, reveal, clearReveal, find } = view;
  // The filter input shows in the sidebar; while the sidebar is collapsed it opens inline.
  const [filterOpen, setFilterOpen] = useState(false);
  // The revision the displayed list reports. Open uses only this pinned revision, so a newer
  // poll never changes the link, and a list without one leaves Open disabled.
  const renderedHeadSha = changes.data?.head_sha ?? null;
  const renderedBaseSha = changes.data?.base_sha ?? null;
  // Commits need the host's capability, a pinned head, and a server that has the routes.
  const commitsUnsupported = usePullRequestCommitsUnsupported(conversationId, prUrl);
  const commitsSupported =
    !!data.capabilities.commit_history && !!prUrl && !!renderedHeadSha && !commitsUnsupported;
  const mode = commitsSupported ? view.mode : "changes";
  const commitsView = mode === "commits";
  const commitView = mode === "commit";
  const commits = usePullRequestCommits(
    conversationId,
    prUrl,
    renderedHeadSha,
    commitsSupported && mode !== "changes",
  );
  // An open commit's patch replaces the PR patch everywhere the diff is read.
  const commitDiff = usePullRequestCommitDiff(
    conversationId,
    prUrl,
    commitView ? view.commitSha : null,
  );
  const shownDiff = commitView ? commitDiff : prDiff;
  // The list is oldest first, so the previous commit is the older one and the next is newer.
  const commitList = commits.data?.data ?? [];
  const commitIndex = commitList.findIndex((commit) => commit.sha === view.commitSha);
  const selectedCommit = commitIndex >= 0 ? commitList[commitIndex] : undefined;
  const previousCommit = commitIndex > 0 ? commitList[commitIndex - 1] : null;
  const nextCommit =
    commitIndex >= 0 && commitIndex < commitList.length - 1 ? commitList[commitIndex + 1] : null;
  // The open commit left the loaded list (the head moved), so there is nothing to step from.
  const { setMode } = view;
  useEffect(() => {
    if (commitView && commits.data && commitIndex < 0) setMode("commits");
  }, [commitView, commits.data, commitIndex, setMode]);
  // A server without the commit routes leaves Changes as the only view, so a saved commit view ends.
  useEffect(() => {
    if (!commitsUnsupported || view.mode === "changes") return;
    setMode("changes");
    toast("Commits need an updated Omnigent server.", { id: "pr-commits-unsupported" });
  }, [commitsUnsupported, view.mode, setMode]);
  const baseRepoUrl = pullRequestRepoUrl(data.provider, pr.url);
  const providerLabel = gitProviderCopy(data.provider, undefined, data.provider_display).label;
  const findInputRef = useRef<HTMLInputElement>(null);
  // A jump or thread target: its header scrolls once the file is visible and its section is mounted.
  const [pendingReveal, setPendingReveal] = useState<{ path: string } | null>(null);
  // The match the reader navigated to. Null until seeded when the bar opens or its query changes.
  const [findTarget, setFindTarget] = useState<Match | null>(null);
  // A match or a pending jump belongs to the patch it was made in, so it ends with a mode or commit change.
  useEffect(() => {
    setFindTarget(null);
    setPendingReveal(null);
  }, [mode, view.commitSha]);
  // The head the shown patch was fetched for. Viewed state needs both the list and the diff
  // pinned to the same head; unpinned providers (GitLab, Azure) can't prove it, so marks are withheld.
  const pinnedHead = changes.data?.head_sha ?? null;
  const renderedHead = pinnedHead && prDiff.data?.head_sha === pinnedHead ? pinnedHead : null;
  // Parse the one whole-PR patch into per-file diffs, keyed by path.
  const prParse = useMemo(
    () => parsePatchByPath(prDiff.data?.patch, PR_PARSE_ERROR),
    [prDiff.data],
  );
  // Viewed marks always hash the whole-PR patch, before the expand-context loader edits these files.
  const patchHashes = useMemo(() => patchFileHashes(prParse.filesByPath), [prParse]);
  const viewedFiles = useViewedFiles(identity, renderedHead, patchHashes);

  const themeType = useResolvedThemeMode();
  // Diff layout is the app-global FileViewer preference (unified/split); seed
  // from the persisted value and write toggles back so the choice carries over.
  const [diffStyle, setDiffStyle] = useState<"unified" | "split">(() =>
    !mobile && readFileViewPreferences().diffLayout === "split" ? "split" : "unified",
  );
  const toggleDiffStyle = useCallback(() => {
    setDiffStyle((prev) => {
      const next = prev === "split" ? "unified" : "split";
      writeFileViewPreferences({ ...readFileViewPreferences(), diffLayout: next });
      return next;
    });
  }, []);

  // The shown patch: the whole PR's parse in Changes, or one commit's.
  const { filesByPath, parseError } = useMemo(
    () => (commitView ? parsePatchByPath(shownDiff.data?.patch, COMMIT_PARSE_ERROR) : prParse),
    [commitView, shownDiff.data, prParse],
  );
  // A commit's list is the files its patch changes; the PR's list is the provider's.
  const commitFiles = useMemo(
    () => (commitView ? changedFilesFromPatch(filesByPath) : []),
    [commitView, filesByPath],
  );
  const listing = useMemo<{
    data?: PullRequestChangedFilesResult;
    isLoading: boolean;
    error: Error | null;
  }>(
    () =>
      commitView
        ? {
            data: { available: true, data: commitFiles, has_more: false },
            isLoading: commitDiff.isLoading,
            error: commitDiff.error,
          }
        : changes,
    [commitView, commitFiles, commitDiff.isLoading, commitDiff.error, changes],
  );
  const files = useMemo<PullRequestChangedFile[]>(() => listing.data?.data ?? [], [listing.data]);
  // Review features (threads, drafts, viewed controls, marks and progress) apply only in Changes.
  const reviewing = mode === "changes";
  // One filtered list drives the tree, sections, threads and collapse all so they never disagree.
  const hiddenPaths = view.hideViewed && reviewing ? viewedFiles.viewed : NO_HIDDEN_PATHS;
  const visibleFiles = useMemo(
    () => selectVisibleFiles(files, { pathFilter: view.pathFilter, hiddenPaths }),
    [files, view.pathFilter, hiddenPaths],
  );
  const viewedCount = files.filter((file) => viewedFiles.viewed.has(file.path)).length;
  // Only changed files the list shows raise the notice; marks on files that left the PR stay quiet.
  const changedListed = files.some((file) => viewedFiles.changed.has(file.path));
  // Hide viewed has emptied every file the path filter still matches.
  const matchingCount = filterChangedPaths(files, view.pathFilter).length;
  const allViewed = view.hideViewed && reviewing && visibleFiles.length === 0 && matchingCount > 0;
  // The sidebar groups the flat file list into a compacted folder tree (the
  // diff scroll below stays linear, in file order).
  const fileTree = useMemo(() => buildSidebarTree(visibleFiles), [visibleFiles]);
  const visiblePaths = useMemo(() => visibleFiles.map((file) => file.path), [visibleFiles]);
  // The toolbar, and with it the find bar, renders only when there is something to show.
  const toolbarShown =
    files.length > 0 ||
    commitsSupported ||
    reviews.isFetching ||
    (reviews.page?.data ?? []).some((t) => !t.resolved);

  // Per-file collapse is lifted here so the toolbar's "expand/collapse all" can
  // drive every section; a path in the set is collapsed unless it is revealed.
  const [collapsedPaths, setCollapsedPaths] = useState<ReadonlySet<string>>(() => new Set());
  // Each view reads and writes only its own collapse and reveal state. A commit's is created
  // fresh for that commit, so nothing a commit does reaches the PR's Changes view.
  const commitState = useCommitViewState(view.commitSha);
  const changesState = useMemo(
    () => ({
      collapsedPaths,
      revealedPaths,
      setCollapsedPaths,
      reveal,
      clearReveal,
    }),
    [collapsedPaths, revealedPaths, reveal, clearReveal],
  );
  const pane = commitView ? commitState : changesState;
  const {
    setCollapsedPaths: setPaneCollapsed,
    clearReveal: clearPaneReveal,
    reveal: revealPane,
  } = pane;
  const isCollapsed = useCallback(
    (path: string) => pane.collapsedPaths.has(path) && !pane.revealedPaths.has(path),
    [pane.collapsedPaths, pane.revealedPaths],
  );
  // Viewed files start collapsed once per rendered head. A collapse that marks introduced
  // expands again on a new head, since its mark was reset; the reader's own collapses stay.
  const [seed, setSeed] = useState<{ head: string | null; owned: ReadonlySet<string> }>({
    head: null,
    owned: new Set(),
  });
  if (viewedFiles.available && renderedHead !== seed.head) {
    // A path is the reader's own collapse unless the previous head's marks collapsed it.
    const owned = new Set(
      [...viewedFiles.viewed].filter((path) => !collapsedPaths.has(path) || seed.owned.has(path)),
    );
    setSeed({ head: renderedHead, owned });
    setCollapsedPaths(
      (prev) =>
        new Set([...prev].filter((path) => !seed.owned.has(path)).concat([...viewedFiles.viewed])),
    );
  }
  // A manual toggle hands the file's collapse to the reader.
  const releaseMarkCollapse = useCallback((paths: Iterable<string>) => {
    setSeed((prev) => {
      const owned = new Set(prev.owned);
      for (const path of paths) owned.delete(path);
      return { ...prev, owned };
    });
  }, []);
  // Hide viewed can remove the file just marked, and with it the focused box. Focus then moves to
  // the next shown file's Viewed box, or to Hide viewed when no file follows.
  const viewedRowRef = useRef<HTMLDivElement>(null);
  const [focusAfterHide, setFocusAfterHide] = useState<{ nextPath: string | null } | null>(null);
  useEffect(() => {
    if (!focusAfterHide) return;
    const nextBox = focusAfterHide.nextPath
      ? scrollRef.current?.querySelector<HTMLElement>(
          `[role="checkbox"][aria-label="${CSS.escape(`Viewed ${focusAfterHide.nextPath}`)}"]`,
        )
      : null;
    (nextBox ?? viewedRowRef.current?.querySelector<HTMLElement>('[role="checkbox"]'))?.focus();
  }, [focusAfterHide]);
  // Marking a file collapses it; unmarking leaves its current state alone.
  const toggleViewed = useCallback(
    (path: string) => {
      const markViewed = !viewedFiles.viewed.has(path);
      viewedFiles.setMany([path], markViewed);
      if (!markViewed) return;
      if (view.hideViewed && reviewing) {
        const index = visibleFiles.findIndex((file) => file.path === path);
        setFocusAfterHide({ nextPath: visibleFiles[index + 1]?.path ?? null });
      }
      if (!collapsedPaths.has(path)) {
        setSeed((prev) => ({ ...prev, owned: new Set(prev.owned).add(path) }));
      }
      clearReveal(path);
      setCollapsedPaths((prev) => (prev.has(path) ? prev : new Set(prev).add(path)));
    },
    [viewedFiles, clearReveal, collapsedPaths, view.hideViewed, reviewing, visibleFiles],
  );

  // Fixed id keeps repeated failures from stacking toasts.
  useEffect(() => {
    if (!viewedFiles.persisted) {
      toast.error("Viewed files can't be saved on this device.", { id: "pr-viewed-persistence" });
    }
  }, [viewedFiles.persisted]);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(mobile);
  // A filter set in the sidebar stays visible once the sidebar collapses.
  useEffect(() => {
    if (!sidebarCollapsed) setFilterOpen(false);
    else if (view.pathFilter !== "") setFilterOpen(true);
  }, [sidebarCollapsed, view.pathFilter]);
  // Hiding the inline filter also clears it, so an active filter is never out of sight.
  const toggleInlineFilter = () => {
    if (filterOpen) view.setPathFilter("");
    setFilterOpen(!filterOpen);
  };
  // Collapsed folders in the sidebar tree (empty = all expanded).
  const [collapsedDirs, setCollapsedDirs] = useState<ReadonlySet<string>>(() => new Set());
  const toggleDir = useCallback((path: string) => {
    setCollapsedDirs((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  // Drag-to-resize the file sidebar via a handle on its right edge.
  const {
    width: sidebarWidth,
    containerRef: bodyRef,
    handleProps: sidebarHandleProps,
  } = useResizableColumn(192, 140, 560);

  // A revealed file collapses on its first toggle, so the reader can always close it.
  const toggleOne = useCallback(
    (path: string) => {
      const collapse = !isCollapsed(path);
      clearPaneReveal(path);
      // Mark ownership belongs to the PR's Changes view; a commit's toggles leave it alone.
      if (reviewing) releaseMarkCollapse([path]);
      setPaneCollapsed((prev) => {
        if (prev.has(path) === collapse) return prev;
        const next = new Set(prev);
        if (collapse) next.add(path);
        else next.delete(path);
        return next;
      });
    },
    [isCollapsed, clearPaneReveal, setPaneCollapsed, reviewing, releaseMarkCollapse],
  );

  const allCollapsed = visibleFiles.length > 0 && visibleFiles.every((f) => isCollapsed(f.path));
  const toggleAll = useCallback(() => {
    for (const file of visibleFiles) clearPaneReveal(file.path);
    if (reviewing) releaseMarkCollapse(visibleFiles.map((file) => file.path));
    setPaneCollapsed((prev) => {
      const next = new Set(prev);
      for (const file of visibleFiles) {
        if (allCollapsed) next.delete(file.path);
        else next.add(file.path);
      }
      return next;
    });
  }, [
    visibleFiles,
    allCollapsed,
    clearPaneReveal,
    setPaneCollapsed,
    reviewing,
    releaseMarkCollapse,
  ]);

  // Expand-context loader: fetch a file's full old/new content on demand when
  // the reader expands unchanged regions.
  const loadDiffFiles = useCallback(
    async (fd: FileDiffMetadata) => {
      // The parser keeps git's quoting; the provider reads files by repository path.
      const path = unquoteGitPath(fd.name);
      const previousPath = fd.prevName ? unquoteGitPath(fd.prevName) : undefined;
      const { before, after } = prUrl
        ? await fetchPullRequestFileContents(conversationId, path, baseRef, {
            pr_url: prUrl,
            previous_path: previousPath,
            head_sha: headSha,
            base_sha: baseSha,
          })
        : await fetchPullRequestFileContents(conversationId, path, baseRef);
      return {
        oldFile: { name: fd.prevName ?? fd.name, contents: before ?? "" },
        newFile: { name: fd.name, contents: after ?? "" },
      };
    },
    [conversationId, baseRef, prUrl, headSha, baseSha],
  );

  const diffOptions = useMemo<DiffOptions>(
    () => ({
      theme: PULL_REQUEST_DIFF_THEME,
      themeType,
      diffStyle,
      unsafeCSS: `
        [data-selected-line] { background-color: var(--sidebar-active); }
        [data-column-number][data-selected-line], [data-gutter-buffer][data-selected-line] {
          color: var(--sidebar-active-foreground);
          box-shadow: inset -3px 0 var(--sidebar-active-foreground);
        }
        ::highlight(pr-find-active) {
          background-color: color-mix(in oklch, var(--warning) 70%, transparent);
          color: inherit;
        }
      `,
      // The section renders its own header; the diff body has none.
      disableFileHeader: true,
      // Expand unchanged context 10 lines at a time (default is 100). The
      // unchanged lines aren't in the patch, so expansion (and the exact
      // trailing-region count) is served by loadDiffFiles on demand.
      expansionLineCount: 10,
      // A commit's unchanged lines need its parent revision, so its context can't expand.
      loadDiffFiles: commitView ? undefined : loadDiffFiles,
    }),
    [themeType, diffStyle, loadDiffFiles, commitView],
  );

  // The stacked diff scrolls as one; the sidebar highlights the file at the top
  // of the viewport and jumps to a file on click.
  const scrollRef = useRef<HTMLDivElement>(null);
  const sectionEls = useRef<Map<string, HTMLElement>>(new Map());
  const [activePath, setActivePath] = useState<string | null>(null);

  const registerRef = useCallback((path: string, el: HTMLElement | null) => {
    if (el) sectionEls.current.set(path, el);
    else sectionEls.current.delete(path);
  }, []);

  const rafRef = useRef<number | null>(null);
  const recomputeActive = useCallback(() => {
    const container = scrollRef.current;
    if (!container) return;
    const top = container.getBoundingClientRect().top;
    let current: string | null = null;
    for (const [path, el] of sectionEls.current) {
      if (el.getBoundingClientRect().top - top <= 8) current = path;
    }
    setActivePath((prev) => current ?? prev);
  }, []);
  const onScroll = useCallback(() => {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      recomputeActive();
    });
  }, [recomputeActive]);

  useEffect(() => {
    setActivePath((prev) =>
      prev && visibleFiles.some((f) => f.path === prev) ? prev : (visibleFiles[0]?.path ?? null),
    );
  }, [visibleFiles]);

  // Jumping to a collapsed file also expands it, so the click lands on its diff.
  const jumpTo = useCallback(
    (path: string) => {
      setActivePath(path);
      revealPane(path);
      setPendingReveal({ path });
    },
    [revealPane],
  );

  // A filtered-out target is visible only after the filter clears. With no filter left to
  // clear it can never show, so drop it rather than scroll to it when a filter changes later.
  useEffect(() => {
    if (!pendingReveal) return;
    if (!visiblePaths.includes(pendingReveal.path)) {
      if (!view.pathFilter && !view.hideViewed) setPendingReveal(null);
      return;
    }
    let frame = 0;
    let frames = 0;
    const scrollToHeader = () => {
      const section = sectionEls.current.get(pendingReveal.path);
      if (section) section.scrollIntoView({ block: "start" });
      if (section || frames++ >= REVEAL_RETRY_FRAMES) setPendingReveal(null);
      else frame = requestAnimationFrame(scrollToHeader);
    };
    scrollToHeader();
    return () => cancelAnimationFrame(frame);
  }, [pendingReveal, visiblePaths, view.pathFilter, view.hideViewed]);

  // Find searches the visible files; the hidden count searches the rest, and no filter changes.
  // Corpora are built only while the bar is open.
  const { matchCase, regex } = find;
  // The commit list has no find bar, so find stays idle there.
  const findOpen = find.open && !commitsView;
  const query = findOpen ? find.query : "";
  // Matches follow the query one render later, so typing stays responsive on large patches.
  const deferredQuery = useDeferredValue(query);
  const searchCorpus = useMemo(
    () => (findOpen ? buildSearchCorpus(filesByPath, visibleFiles) : []),
    [findOpen, filesByPath, visibleFiles],
  );
  const hiddenCorpus = useMemo(() => {
    if (!findOpen) return [];
    const shown = new Set(visiblePaths);
    return buildSearchCorpus(
      filesByPath,
      files.filter((file) => !shown.has(file.path)),
    );
  }, [findOpen, filesByPath, files, visiblePaths]);
  const search = useMemo(
    () => findMatches(searchCorpus, deferredQuery, { matchCase, regex, limit: MATCH_LIMIT }),
    [searchCorpus, deferredQuery, matchCase, regex],
  );
  const hiddenSearch = useMemo(
    () => findMatches(hiddenCorpus, deferredQuery, { matchCase, regex, limit: MATCH_LIMIT }),
    [hiddenCorpus, deferredQuery, matchCase, regex],
  );
  const { matches } = search;
  // The match shown after the last render. A refresh measures from it, so repeated refreshes
  // follow the occurrence the reader is on rather than the original target.
  const shownMatch = useRef<{ target: Match; shown: Match } | null>(null);
  const activeIndex =
    findTarget && matches.length
      ? refreshedMatchIndex(
          matches,
          shownMatch.current?.target === findTarget ? shownMatch.current.shown : findTarget,
        )
      : -1;
  const activeMatch = activeIndex >= 0 ? matches[activeIndex] : null;
  useEffect(() => {
    shownMatch.current =
      findTarget && activeMatch ? { target: findTarget, shown: activeMatch } : null;
  });
  const activeFilePath = activeMatch?.target.path ?? null;
  // A line match is painted by its section; split out the primitives so a refresh that
  // keeps the same match doesn't re-centre the row.
  const lineMatch = activeMatch?.target.kind === "line" ? activeMatch : null;
  const highlightSide = lineMatch?.target.side;
  const highlightLine = lineMatch?.target.line;
  const highlightStart = lineMatch?.start;
  const highlightEnd = lineMatch?.end;
  const highlight = useMemo<FindHighlight | null>(
    () =>
      findTarget &&
      highlightSide &&
      highlightLine !== undefined &&
      highlightStart !== undefined &&
      highlightEnd !== undefined
        ? { side: highlightSide, line: highlightLine, start: highlightStart, end: highlightEnd }
        : null,
    // findTarget is new on each explicit navigation, so a repeat step re-centres the row.
    [findTarget, highlightSide, highlightLine, highlightStart, highlightEnd],
  );

  // Seed the match when the bar opens or its query changes: the first at or below the
  // viewport top. A path match also scrolls its header, as stepping to it does.
  useEffect(() => {
    if (!findOpen || findTarget || !matches.length || deferredQuery !== query) return;
    const seeded = matches[firstMatchAtOrBelow(matches, visiblePaths, activePath)];
    setFindTarget(seeded);
    if (seeded.target.kind === "path") setPendingReveal({ path: seeded.target.path });
  }, [findOpen, findTarget, matches, visiblePaths, activePath, deferredQuery, query]);

  // The active match's file opens even if the reader collapsed it earlier.
  useEffect(() => {
    if (activeFilePath && !commitsView) revealPane(activeFilePath);
  }, [activeFilePath, findTarget, revealPane, commitsView]);

  const stepMatch = (direction: 1 | -1) => {
    // Matches for a query still being searched would navigate the previous results.
    if (!matches.length || deferredQuery !== query) return;
    // With no active match yet, the first step lands on the first or last match by direction.
    const next = matches[stepTarget(activeIndex, direction, matches.length)];
    // A fresh object, so stepping to the sole match again is a new reveal request.
    setFindTarget({ ...next });
    if (next.target.kind === "path") setPendingReveal({ path: next.target.path });
  };
  // Changing the query or an option re-seeds the match from the viewport.
  const changeFind = (patch: Partial<FindState>) => {
    setFindTarget(null);
    view.setFind(patch);
  };
  const closeFind = () => {
    setFindTarget(null);
    view.setFind({ open: false });
  };
  const showHidden = () => {
    view.setPathFilter("");
    // Hide viewed belongs to the whole-PR view, so a commit view leaves it as it is.
    if (reviewing) view.setHideViewed(false);
  };
  const onTabKeyDown = (event: React.KeyboardEvent) => {
    // The platform's command modifier only: Ctrl+F stays the caret-forward key on macOS.
    // IME composition keys confirm text, not find.
    // The commit list has no find bar, so the shortcut stays with the browser there.
    if (
      !toolbarShown ||
      commitsView ||
      event.nativeEvent.isComposing ||
      event.keyCode === 229 ||
      !hasCommandModifier(event) ||
      event.altKey ||
      event.shiftKey ||
      event.key.toLowerCase() !== "f"
    ) {
      return;
    }
    event.preventDefault();
    if (!find.open) view.setFind({ open: true });
    else findInputRef.current?.select();
    findInputRef.current?.focus();
  };
  const patchFileCount = files.filter((file) => filesByPath.has(file.path)).length;
  const withoutText = files.length - patchFileCount;
  // Counts are meaningless until the patch has loaded and parsed.
  const patchReady = !!shownDiff.data?.patch && !parseError && !shownDiff.isLoading;
  // A disabled commit query has no data, which is not a commit without changes.
  const commitUnavailable =
    commitView && !commitDiff.data && !commitDiff.isLoading && !commitDiff.error;
  const emptyChangesMessage = emptyChangesText(commitView, commitUnavailable);
  const coverageNote =
    findOpen && patchReady && (listing.data?.has_more || withoutText > 0)
      ? `Search covers the fetched patch: ${patchFileCount} of ${listing.data?.total_count ?? files.length} files${withoutText > 0 ? `, ${withoutText} without text` : ""}`
      : null;

  // Header actions for one file. Deleted files open at the base revision; others
  // at the rendered head, so a newer polled head never changes the link. A commit's
  // files open at that commit, and a file the commit deletes has no page to open.
  const fileActions = (file: PullRequestChangedFile) => {
    const previous = file.previous_path ?? filesByPath.get(file.path)?.prevName ?? null;
    const renamedFrom = previous && previous !== file.path ? previous : null;
    const sourceRepoUrl = file.status === "deleted" ? baseRepoUrl : pr.head_repository_url;
    const openUrl = pullRequestFileUrl({
      provider: data.provider,
      headRepoUrl: pr.head_repository_url,
      baseRepoUrl,
      headSha: commitView ? view.commitSha : renderedHeadSha,
      baseSha: commitView ? null : renderedBaseSha,
      path: file.path,
      previousPath: renamedFrom,
      status: file.status,
    });
    return (
      <PullRequestFileActions
        path={file.path}
        previousPath={renamedFrom}
        openLabel={`Open in ${providerLabel}`}
        openUrl={openUrl}
        openUnavailableReason={
          commitView && file.status === "deleted"
            ? "Deleted in this commit"
            : openUnavailableReason(providerLabel, !!baseRepoUrl, sourceRepoUrl)
        }
      />
    );
  };

  const [focusedThread, setFocusedThread] = useState<{ id: string } | null>(null);
  useEffect(() => {
    const container = scrollRef.current;
    if (!container || !focusedThread || !selected) return;
    const focusThread = () => {
      const element = container.querySelector<HTMLElement>(
        `[data-review-thread="${CSS.escape(focusedThread.id)}"]`,
      );
      if (!element || !element.getClientRects().length) return;
      element.scrollIntoView({ block: "center" });
      element.focus({ preventScroll: true });
      observer.disconnect();
    };
    const observer = new MutationObserver(focusThread);
    observer.observe(container, { childList: true, subtree: true });
    focusThread();
    return () => observer.disconnect();
  }, [focusedThread, selected]);

  const threadContext: ReviewThreadContext = {
    conversationId,
    prUrl: data.selected_pr_url ?? pr.url,
    account: data.auth.selected_account,
    canReply: !!(canEdit && data.capabilities.review_thread_replies),
    canCreate: !!(
      canEdit &&
      data.capabilities.review_thread_creation &&
      pr.state === "OPEN" &&
      pr.head_sha &&
      pr.base_sha &&
      pr.head_sha === prDiff.data?.head_sha &&
      pr.base_sha === prDiff.data?.base_sha &&
      reviews.page
    ),
    headSha: pr.head_sha,
    baseSha: pr.base_sha,
    loadComments: reviews.loadComments,
    loadingComments: reviews.loadingComments,
  };
  // Review conversations belong to the PR's head, so a commit's patch shows none.
  const threads = reviewing ? (reviews.page?.data ?? []) : [];
  const matchingRevision =
    !!reviews.page &&
    reviews.page.head_sha === prDiff.data?.head_sha &&
    reviews.page.base_sha === prDiff.data?.base_sha;
  const canShowFiles =
    !listing.error &&
    !shownDiff.error &&
    !parseError &&
    !shownDiff.data?.unavailable_reason &&
    files.length > 0 &&
    !shownDiff.isLoading;
  const threadsByFile = new Map<string, ReviewThread[]>();
  const earlierThreads: ReviewThread[] = [];
  for (const thread of threads) {
    const file =
      canShowFiles &&
      files.find(
        (entry) =>
          entry.path === thread.path || filesByPath.get(entry.path)?.prevName === thread.path,
      );
    if (!file) earlierThreads.push(thread);
    // A thread on a file the path filter or hide-viewed hides shows again once they clear.
    else if (visibleFiles.includes(file))
      threadsByFile.set(file.path, [...(threadsByFile.get(file.path) ?? []), thread]);
  }
  const unresolved = threads.filter((thread) => !thread.resolved);
  const navigateThread = (direction: number) => {
    const index = unresolved.findIndex((thread) => thread.id === focusedThread?.id);
    const start = index < 0 ? (direction > 0 ? -1 : 0) : index;
    const next = unresolved[(start + direction + unresolved.length) % unresolved.length];
    if (!next) return;
    const file = files.find(
      (entry) => entry.path === next.path || filesByPath.get(entry.path)?.prevName === next.path,
    );
    if (file) {
      // A thread on a file a filter hides has no mounted section, so clear that filter first.
      // The path filter goes only when it hides the file; hide-viewed goes only for a viewed one.
      if (!visibleFiles.includes(file)) {
        const pathHides = filterChangedPaths([file], view.pathFilter).length === 0;
        const viewedHides = view.hideViewed && viewedFiles.viewed.has(file.path);
        if (pathHides) view.setPathFilter("");
        if (viewedHides) view.setHideViewed(false);
        toast(`Filter cleared to show ${file.path}`);
      }
      jumpTo(file.path);
    }
    setFocusedThread({ id: next.id });
  };

  // A controls row, then the sidebar (jump-to-file) + one scroll of all files' diffs.
  return (
    <TabsContent value="changes" className="flex min-h-0 flex-1 flex-col" onKeyDown={onTabKeyDown}>
      {reviewing &&
        data.capabilities.review_threads &&
        (reviews.page?.has_more ||
          reviews.historyStale ||
          reviews.page?.warning ||
          reviews.error) && (
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b px-3 py-1 text-ui">
            {reviews.page?.has_more && (
              <Button
                variant="ghost"
                size="sm"
                disabled={reviews.loadingMore}
                onClick={reviews.loadMore}
              >
                {reviews.loadingMore ? "Loading…" : "Load more conversations"}
              </Button>
            )}
            {reviews.historyStale && (
              <span className="text-muted-foreground">
                Older pages may have changed.{" "}
                <button type="button" className="underline" onClick={reviews.refreshHistory}>
                  Reload conversations
                </button>
              </span>
            )}
            {(reviews.page?.warning || reviews.error) && (
              <span role="status" className="text-destructive">
                {reviews.error ?? reviews.page?.warning}
              </span>
            )}
          </div>
        )}
      {!commitsView && (listing.data?.has_more || listing.data?.warning) && (
        <p
          role="status"
          className="shrink-0 border-b border-border p-2 text-ui text-muted-foreground"
        >
          {listing.data.warning || "The changed-file list is incomplete."}
        </p>
      )}
      {coverageNote && (
        <p className="shrink-0 border-b border-border px-2 py-1 text-xs text-muted-foreground">
          {coverageNote}
        </p>
      )}
      {reviewing && changedListed && view.dismissedResetHead !== renderedHead && (
        <div
          role="status"
          className="flex shrink-0 items-center justify-between gap-2 border-b border-border px-3 py-1 text-ui text-muted-foreground"
        >
          New commits since you last viewed — changed files lost their viewed marks.
          <Button
            variant="ghost"
            size="sm"
            onClick={() => view.setDismissedResetHead(renderedHead)}
          >
            Dismiss
          </Button>
        </div>
      )}
      {toolbarShown && (
        <ChangesToolbar
          showFileControls={files.length > 0 && !commitsView}
          sidebarCollapsed={sidebarCollapsed}
          onToggleSidebar={() => setSidebarCollapsed((v) => !v)}
          filterOpen={filterOpen}
          onToggleFilter={
            files.length > 0 && sidebarCollapsed && !commitsView ? toggleInlineFilter : undefined
          }
          canNavigateThreads={unresolved.length > 0}
          onNavigateThread={navigateThread}
          loadingThreads={reviewing && reviews.isFetching}
          diffStyle={diffStyle}
          onToggleDiffStyle={toggleDiffStyle}
          allCollapsed={allCollapsed}
          onToggleAll={toggleAll}
          modeSwitch={
            commitsSupported ? (
              <PullRequestChangesModeSwitch
                value={commitsView || commitView ? "commits" : "changes"}
                onChange={view.setMode}
              />
            ) : undefined
          }
          navigation={
            commitsView ? undefined : (
              <PullRequestFindBar
                find={find}
                inputRef={findInputRef}
                position={activeIndex + 1}
                total={matches.length}
                truncated={search.truncated}
                invalid={search.invalid}
                hiddenCount={hiddenSearch.matches.length}
                hiddenTruncated={hiddenSearch.truncated}
                onOpen={() => view.setFind({ open: true })}
                onClose={closeFind}
                onQueryChange={(next) => changeFind({ query: next })}
                onToggleOption={(option) =>
                  changeFind(option === "matchCase" ? { matchCase: !matchCase } : { regex: !regex })
                }
                onStep={stepMatch}
                onShowHidden={showHidden}
              />
            )
          }
          progress={
            files.length > 0 && viewedFiles.available && reviewing ? (
              <PullRequestViewedProgress
                viewedCount={viewedCount}
                totalCount={files.length}
                partial={!!changes.data?.has_more}
                mobile={mobile}
                hideViewed={view.hideViewed}
                onHideViewedChange={view.setHideViewed}
                rowRef={viewedRowRef}
              />
            ) : undefined
          }
        />
      )}
      {/* With the sidebar collapsed (narrow screens), the filter opens above the list. */}
      {sidebarCollapsed && files.length > 0 && filterOpen && !commitsView && (
        <div className="shrink-0 border-b border-border p-2">
          <PullRequestPathFilter value={view.pathFilter} onChange={view.setPathFilter} />
        </div>
      )}
      {commitView && (
        <PullRequestCommitBar
          sha={view.commitSha ?? ""}
          title={selectedCommit?.title ?? null}
          previous={previousCommit}
          next={nextCommit}
          onBack={() => view.setMode("commits")}
          onOpen={view.openCommit}
        />
      )}
      <div ref={bodyRef as React.RefObject<HTMLDivElement>} className="flex min-h-0 flex-1">
        {!sidebarCollapsed && !commitsView && (
          <ChangesSidebar
            width={sidebarWidth}
            handleProps={sidebarHandleProps}
            filter={
              files.length > 0 && (
                <div className="p-2 pr-3">
                  <PullRequestPathFilter value={view.pathFilter} onChange={view.setPathFilter} />
                </div>
              )
            }
          >
            {listing.isLoading ? (
              <div className="flex items-center justify-center p-4 text-muted-foreground">
                <Loader2Icon className="size-4 animate-spin" />
              </div>
            ) : listing.error ? (
              <p className="px-2 py-1 text-ui text-muted-foreground">
                {readErrorText(listing.error)}
              </p>
            ) : files.length === 0 ? (
              <p className="px-2 py-1 text-ui text-muted-foreground">
                {listing.data?.available === false ||
                listing.data?.has_more ||
                listing.data?.warning
                  ? "Changed files are unavailable."
                  : emptyChangesMessage}
              </p>
            ) : (
              fileTree.map((node) => (
                <SidebarNode
                  key={node.type === "file" ? node.file.path : node.path}
                  node={node}
                  depth={0}
                  activePath={activePath}
                  onSelectFile={jumpTo}
                  collapsedDirs={collapsedDirs}
                  onToggleDir={toggleDir}
                  viewedPaths={reviewing ? viewedFiles.viewed : NO_HIDDEN_PATHS}
                />
              ))
            )}
          </ChangesSidebar>
        )}
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="github-diff-stack min-w-0 flex-1 overflow-y-auto"
        >
          {commitsView ? (
            <PullRequestCommitsList
              commits={commitList}
              hasMore={!!commits.data?.has_more}
              loading={commits.isLoading}
              // A disabled query has no data, and that is not the same as an empty history.
              unavailable={commits.data === undefined && !commits.isLoading && !commits.error}
              errorMessage={commits.error ? readErrorText(commits.error) : null}
              query={view.commitQuery}
              onQueryChange={view.setCommitQuery}
              onSelect={view.openCommit}
            />
          ) : listing.error || shownDiff.error || parseError ? (
            <PanelMessage>
              <p role="alert">
                {parseError || readErrorText((listing.error || shownDiff.error) as Error)}
              </p>
            </PanelMessage>
          ) : shownDiff.data?.unavailable_reason ? (
            <PanelMessage>
              {shownDiff.data.message ||
                (shownDiff.data.unavailable_reason === "pr_outside_workspace"
                  ? "Diff unavailable for a PR outside this workspace's repository"
                  : "Diff unavailable. Refresh or open the request on its provider.")}
            </PanelMessage>
          ) : files.length === 0 || shownDiff.isLoading ? (
            <PanelMessage>
              {listing.isLoading || shownDiff.isLoading ? (
                <>
                  <Loader2Icon className="size-5 animate-spin" />
                  Loading changes…
                </>
              ) : listing.data?.available === false ||
                listing.data?.has_more ||
                listing.data?.warning ? (
                "Changed files are unavailable."
              ) : (
                emptyChangesMessage
              )}
            </PanelMessage>
          ) : allViewed ? (
            <PanelMessage>
              <span>{`All ${matchingCount} files viewed`}</span>
              <Button variant="outline" size="sm" onClick={() => view.setHideViewed(false)}>
                Show viewed
              </Button>
            </PanelMessage>
          ) : visibleFiles.length === 0 ? (
            <PanelMessage>
              <p>{`No changed files match "${view.pathFilter.trim()}"`}</p>
              <Button variant="ghost" size="sm" onClick={() => view.setPathFilter("")}>
                Clear filter
              </Button>
            </PanelMessage>
          ) : (
            visibleFiles.map((file) => (
              <PullRequestFileSection
                key={file.path}
                file={file}
                fileDiff={filesByPath.get(file.path)}
                options={diffOptions}
                registerRef={registerRef}
                collapsed={pane.collapsedPaths.has(file.path)}
                forceExpanded={pane.revealedPaths.has(file.path)}
                forceMount={pendingReveal?.path === file.path || activeFilePath === file.path}
                bypassLargeDiffGate={!!highlight && activeFilePath === file.path}
                highlight={activeFilePath === file.path ? highlight : null}
                onToggleCollapsed={() => toggleOne(file.path)}
                headerActions={fileActions(file)}
                reviewable={reviewing}
                reviewActions={
                  viewedFiles.available ? (
                    <PullRequestViewedToggle
                      path={file.path}
                      viewed={viewedFiles.viewed.has(file.path)}
                      changed={viewedFiles.changed.has(file.path)}
                      onToggle={() => toggleViewed(file.path)}
                    />
                  ) : undefined
                }
                threads={threadsByFile.get(file.path) ?? []}
                threadContext={threadContext}
                matchingRevision={matchingRevision}
              />
            ))
          )}
          {earlierThreads.length > 0 && (
            <div className="p-3">
              <p className="text-ui font-medium">Earlier conversations and unavailable diffs</p>
              {earlierThreads.map((thread) => (
                <div key={thread.id}>
                  <p className="mt-3 break-all text-xs text-muted-foreground">{thread.path}</p>
                  <PullRequestReviewThread thread={thread} context={threadContext} showContext />
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </TabsContent>
  );
}

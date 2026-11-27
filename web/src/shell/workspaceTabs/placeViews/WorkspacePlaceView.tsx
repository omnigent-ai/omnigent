// The body of a Files, Changes or pull request tab in the workspace sidebar. Each
// renders its view from the tab's place and turns clicks inside it into in-tab steps
// (onNavigate) or opens elsewhere (onOpen), so Back returns and the tab's title always
// follows what it shows.

import { SearchIcon } from "lucide-react";
import { lazy, Suspense, useCallback, useMemo } from "react";

import { Button } from "@/components/ui/button";
import { CompactShortcutKeys } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";
import { shortcutKeys } from "@/lib/shortcuts/registry";
import { requestFileFinder, useFileFinderAvailable } from "@/store/fileFinder";
import { useShowIgnoredFiles, writeShowIgnoredFiles } from "@/lib/filesTreeIgnoredPreferences";
import type { ChangedSort } from "@/lib/changedSort";
import type { CommentSelectionRequest } from "@/shell/commentSelectionHandoff";
import { useFilesTreePageCommands } from "@/shell/fileTree/useFilesTreePageCommands";
import { useFilesTreeShowRequests } from "@/shell/fileTree/treeRoot";
import { isFilePositionPending } from "@/shell/filePositionState";
import type { FilePosition } from "@/shell/FileViewerContext";
import { PullRequestPanel } from "@/shell/PullRequestPanel";
import { PullRequestPageBody } from "@/pullRequestPage/PullRequestPageBody";

import type { OpenOptions } from "../planOpen";
import type { TabLanding } from "../workspaceTabsStore";
import {
  WORKSPACE_SIDEBAR_LAYOUT,
  WORKSPACE_TOOLBAR_CLASS,
  WorkspaceSidebarLayout,
  WorkspaceSidebarToggle,
  useWorkspaceSidebarWidth,
  useWorkspaceSidebar,
} from "../WorkspaceSidebarLayout";
import type { LineRange, WorkspaceLink, WorkspacePlace } from "../workspacePlace";
import {
  changedFileViewed,
  changesComparisonShown,
  changesDefaultComparison,
  changesFileInNewTab,
  changesOverview,
  fileTabFileChosen,
  fileViewDiffRequested,
  allPullRequestsChosen,
  pullRequestChosen,
  pullRequestSectionChosen,
  shownPullRequestPlace,
  treeFileChosen,
  unshownPullRequestParts,
  type ChangesPlace,
  type FilesPlace,
  type PlaceViewAction,
  type PullRequestPlace,
} from "./placeViewActions";
import { FILES_TREE_WIDTH } from "./filesTreeWidth";
import { FilesPageTree } from "./FilesPageTree";
import { useFilesTreeOptionCommands } from "./filesTreeOptions";
import { type FilesPageLeaveGuard, FilesPageViewer } from "./FilesPageViewer";
import {
  HIDE_FILES_TREE_LABEL,
  SHOW_FILES_TREE_LABEL,
  useFilesTreeToggleCommand,
} from "./useFilesTreeToggleCommand";

// Loaded only where the Diffs tab setting picks Changes review, with the review it shares.
const ChangesReview = lazy(() =>
  import("@/shell/ChangesReview").then((module) => ({ default: module.ChangesReview })),
);

/** Changed-file list settings the sidebar keeps, shared with the Classic panels. */
export interface PlaceViewFileSettings {
  sort: ChangedSort;
  onSortChange: (sort: ChangedSort) => void;
  showHidden: boolean;
  onShowHiddenChange: (showHidden: boolean) => void;
  permissionLevel?: number | null;
}

export interface WorkspacePlaceViewProps {
  conversationId: string;
  place: FilesPlace | ChangesPlace | PullRequestPlace;
  /** How the tab last landed on `place`, by a step, a link or an open. */
  landing?: TabLanding;
  /**
   * This is the session's Files tab, which browses with its tree. Other file tabs
   * show just their file, so a tree click can't land in a different tab.
   */
  filesTab: boolean;
  /** The tab is selected and the sidebar is open. */
  active: boolean;
  /** Whether app-wide shortcuts and ⌘K rows act here, as the panel's keyboard target. */
  keyboardTarget?: boolean;
  /** Move this tab to `place`: a step in its history, or a correction with `replace`. */
  onNavigate: (place: WorkspacePlace, options: { replace: boolean }) => void;
  /** Open a place elsewhere through the tab store's open rules. */
  onOpen: (link: WorkspaceLink, options: OpenOptions) => void;
  /** Open a file to start a comment on a selection made here, such as in the Changes review. */
  onCommentOnFile?: (request: CommentSelectionRequest) => void;
  files: PlaceViewFileSettings;
  /** A citation's position for a file, which the tab's request gives; else the place's line. */
  position?: FilePosition;
  /** Registers the question the tab asks before it leaves a file's unsaved edits. */
  registerLeaveGuard?: (guard: FilesPageLeaveGuard) => () => void;
}

/** Places this view renders; the sidebar renders the others itself. */
export function isPlaceViewPlace(place: WorkspacePlace): place is WorkspacePlaceViewProps["place"] {
  return (
    place.kind === "files" ||
    place.kind === "changes" ||
    place.kind === "pulls" ||
    place.kind === "pull"
  );
}

function useRunAction(
  onNavigate: WorkspacePlaceViewProps["onNavigate"],
  onOpen: WorkspacePlaceViewProps["onOpen"],
) {
  return useCallback(
    (action: PlaceViewAction) => {
      if (action.type === "navigate") onNavigate(action.place, { replace: action.replace });
      else if (action.type === "open") onOpen(action.link, action.options);
    },
    [onNavigate, onOpen],
  );
}

/** Remembers, per device, whether the Files tab docks its tree. */
const FILES_TREE_VISIBLE_STORAGE_KEY = "omnigent:files-tree-visible";

/** One position object per line range, so the viewer reveals a citation once. */
function usePosition(line: LineRange | undefined): FilePosition | undefined {
  const start = line?.start;
  const end = line?.end;
  const column = line?.column;
  return useMemo(
    () =>
      start === undefined
        ? undefined
        : {
            line: start,
            ...(end !== undefined ? { endLine: end } : {}),
            ...(column !== undefined ? { column } : {}),
          },
    [start, end, column],
  );
}

export function WorkspacePlaceView(props: WorkspacePlaceViewProps) {
  switch (props.place.kind) {
    case "files":
      return <FilesPlaceView {...props} place={props.place} />;
    case "changes":
      return <ChangesPlaceView {...props} place={props.place} />;
    default:
      return <PullRequestPlaceView {...props} place={props.place} />;
  }
}

function FilesPlaceView({
  conversationId,
  place,
  active,
  onNavigate,
  onOpen,
  files,
  position,
  registerLeaveGuard,
  filesTab,
  keyboardTarget = true,
}: WorkspacePlaceViewProps & { place: FilesPlace }) {
  // A visible place shows its file; only the keyboard target's answers its chords and ⌘K rows.
  const commandsActive = active && keyboardTarget;
  const run = useRunAction(onNavigate, onOpen);
  const placePosition = usePosition(place.line);
  const shownPosition = position ?? placePosition;
  // With no file open, a narrow Files tab is the tree itself rather than an empty page.
  const tree = useWorkspaceSidebar(FILES_TREE_VISIBLE_STORAGE_KEY, {
    fillWhenNarrow: place.path === undefined,
  });
  const treeWidth = useWorkspaceSidebarWidth(tree, FILES_TREE_WIDTH);
  useFilesTreeToggleCommand(tree, commandsActive);
  const goToFileKeys = shortcutKeys("palette.files");
  const fileFinderAvailable = useFileFinderAvailable(conversationId);
  // ⌘K reveals the file and roots the tree from here, so both stay while it is hidden.
  useFilesTreePageCommands({
    conversationId,
    activePath: place.path ?? null,
    showHidden: files.showHidden,
    enabled: commandsActive,
  });
  useFilesTreeShowRequests(active ? conversationId : undefined, tree.show);
  // Git-ignored files are a per-device choice of the Files tree, beside the panel's settings.
  const showIgnored = useShowIgnoredFiles();
  const treeOptions = { ...files, showIgnored, onShowIgnoredChange: writeShowIgnoredFiles };
  useFilesTreeOptionCommands(treeOptions, commandsActive);
  // Every Files place, the Files tab or a file tab, shows the same tree beside its file; the
  // tree's options sit in its own header.
  const treeToggle = (
    <WorkspaceSidebarToggle
      state={tree}
      showLabel={SHOW_FILES_TREE_LABEL}
      hideLabel={HIDE_FILES_TREE_LABEL}
    />
  );
  // A file picked in the tree or the breadcrumbs: a file tab steps to it, the Files tab opens it.
  const openPickedFile = (path: string, options?: { newTab?: boolean }) =>
    run(
      filesTab || options?.newTab
        ? treeFileChosen(path, { newTab: options?.newTab })
        : fileTabFileChosen(place, path),
    );
  return (
    <WorkspaceSidebarLayout
      data-testid="workspace-files-place"
      state={tree}
      resize={treeWidth}
      sidebarLabel="Workspace files"
      sidebar={
        <FilesPageTree
          conversationId={conversationId}
          active={active}
          commandsActive={commandsActive}
          activePath={place.path}
          sort={files.sort}
          onSortChange={files.onSortChange}
          showHidden={files.showHidden}
          onShowHiddenChange={files.onShowHiddenChange}
          showIgnored={showIgnored}
          onShowIgnoredChange={writeShowIgnoredFiles}
          onFileSelect={(path, options) => {
            tree.dismissPopover();
            openPickedFile(path, options);
          }}
        />
      }
    >
      {place.path === undefined ? (
        <div className={cn("flex flex-col bg-background", WORKSPACE_SIDEBAR_LAYOUT.page)}>
          <div className={cn(WORKSPACE_TOOLBAR_CLASS, WORKSPACE_SIDEBAR_LAYOUT.toolbar)}>
            {treeToggle}
          </div>
          <div
            className={cn(
              "flex flex-col items-center justify-center gap-1 px-6 text-center",
              WORKSPACE_SIDEBAR_LAYOUT.content,
            )}
          >
            <p className="text-ui font-medium text-foreground">Open a file</p>
            <p className="text-ui text-muted-foreground">
              Pick a file in the tree, or search for one.
            </p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-2"
              disabled={!fileFinderAvailable}
              onClick={() => requestFileFinder(conversationId)}
            >
              <SearchIcon />
              Go to file
              {goToFileKeys.length > 0 ? <CompactShortcutKeys keys={goToFileKeys} /> : null}
            </Button>
          </div>
        </div>
      ) : (
        <FilesPageViewer
          key={place.path}
          conversationId={conversationId}
          path={place.path}
          position={shownPosition}
          active={active}
          toolbarLeading={treeToggle}
          registerLeaveGuard={registerLeaveGuard}
          permissionLevel={files.permissionLevel}
          commentId={place.comment}
          onOpenFile={(path) => openPickedFile(path)}
          // The Files tab closes its file back to the tree; another file tab goes to the Files tab.
          onClose={() =>
            filesTab
              ? onNavigate({ kind: "files" }, { replace: false })
              : onOpen({ place: { kind: "files" } }, { opener: "user" })
          }
          onShowChanges={() =>
            run(
              fileViewDiffRequested(
                place,
                true,
                place.path!,
                isFilePositionPending(shownPosition) ? shownPosition : undefined,
              ),
            )
          }
        />
      )}
    </WorkspaceSidebarLayout>
  );
}

/** The Diffs tab: Changes review over every changed file. */
function ChangesPlaceView({
  conversationId,
  place,
  active,
  onNavigate,
  onOpen,
  onCommentOnFile,
  files,
  position,
  landing,
}: WorkspacePlaceViewProps & { place: ChangesPlace }) {
  const run = useRunAction(onNavigate, onOpen);
  const range = place.range;
  // A link's own request when there is one, so citing a line again reveals it again.
  const linePosition = usePosition(place.line);
  const revealPosition = position ?? linePosition;
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="workspace-changes-place">
      <Suspense fallback={null}>
        <ChangesReview
          conversationId={conversationId}
          revealPath={place.path}
          revealPosition={revealPosition}
          requestedRange={
            range ? { base: range.base, head: range.head ?? "Working Copy" } : undefined
          }
          comparison={
            place.comparison ??
            (range ? { kind: "typed", base: range.base, head: range.head ?? "" } : undefined)
          }
          comparisonRoot={place.comparisonRoot ?? null}
          onComparisonShown={(shown, options) => run(changesComparisonShown(place, shown, options))}
          landing={landing}
          active={active}
          sort={files.sort}
          onViewFile={(path) => run(changedFileViewed(path))}
          onShowAllChanges={() => run(changesOverview(place))}
          onShowDefaultComparison={() => run(changesDefaultComparison(place))}
          onCommentOnFile={onCommentOnFile}
          onOpenFileInNewTab={(path, options) => run(changesFileInNewTab(place, path, options))}
        />
      </Suspense>
    </div>
  );
}

function PullRequestPlaceView({
  conversationId,
  place,
  active,
  onNavigate,
  onOpen,
}: WorkspacePlaceViewProps & { place: PullRequestPlace }) {
  const run = useRunAction(onNavigate, onOpen);
  const unshown = unshownPullRequestParts(place);
  return (
    <div
      className="flex min-h-0 flex-1 flex-col"
      data-testid="workspace-pull-request-place"
      // A two-finger swipe in a sidebar PR tab steps the sidebar's history, as ⌘← / ⌘→ do here.
      data-swipe-history="sidebar"
    >
      {unshown && (
        <div
          role="status"
          className="flex shrink-0 items-center gap-2 border-b border-border bg-muted/50 px-3 py-1.5 text-ui text-muted-foreground"
        >
          <span className="min-w-0 flex-1">{unshown}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onNavigate(shownPullRequestPlace(place), { replace: true })}
          >
            Show what&apos;s here
          </Button>
        </div>
      )}
      {place.kind === "pulls" ? (
        <PullRequestPanel
          conversationId={conversationId}
          active={active}
          onOpenPullRequest={(url, section) => run(pullRequestChosen(url, section))}
        />
      ) : (
        <PullRequestPageBody
          key={place.url}
          conversationId={conversationId}
          prUrl={place.url}
          section={place.view}
          active={active}
          onSectionChange={(section) => run(pullRequestSectionChosen(place, section))}
          onShowAllPullRequests={() => run(allPullRequestsChosen())}
        />
      )}
    </div>
  );
}

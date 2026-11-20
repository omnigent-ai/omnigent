// A whole-review diff: every changed file in one virtualized, scrolling list
// beside a tree of changed files, with filters, collapse controls, and find
// across all files. Sources load the files; this component only presents them.

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type ReactNode,
} from "react";
import { DIFF_PALETTE_CSS, DIFF_SYNTAX_THEME } from "@/lib/diffTheme";
import type {
  AnnotationSide,
  CodeViewItem,
  DiffLineAnnotation,
  LineAnnotation,
  FileDiffContentsLoader,
  FileDiffMetadata,
  SelectedLineRange,
  SupportedLanguages,
  CodeViewScrollListener,
} from "@pierre/diffs";
import {
  CodeView,
  WorkerPoolContext,
  WorkerPoolContextProvider,
  type CodeViewHandle,
} from "@pierre/diffs/react";
import {
  BookOpenTextIcon,
  CheckCheckIcon,
  CheckIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  CopyIcon,
  InfoIcon,
  MoreHorizontalIcon,
  RotateCwIcon,
  SquarePlusIcon,
  UserCheckIcon,
  XIcon,
} from "lucide-react";
import type { DiffReviewFileAction } from "./diffReviewFileAction";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { changedFileCommands } from "./changedFileCommands";
import { newTabFileAction } from "./newTabFileAction";
import { FindBar } from "@/components/FindBar";
import { PaneResizeHandle } from "@/components/PaneResizeHandle";
import { hiddenMatchesNote } from "@/components/hiddenMatchesNote";
import type { SearchHighlightNames } from "@/lib/textRangeSearch";
import { SessionActionMenuItem } from "@/components/SessionActionMenuItem";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { cn } from "@/lib/utils";
import { useRememberedPaneWidth } from "@/hooks/useRememberedPaneWidth";
import {
  codeFontFamilyForEditor,
  readCodeFont,
  subscribeCodeFont,
} from "@/lib/codeFontPreferences";
import {
  AVAILABLE,
  type PaletteCommand,
  unavailableBecause,
  usePaletteCommands,
} from "@/store/paletteCommands";
import {
  diffReviewCommands,
  diffReviewPaletteCommands,
  ownedFilesCommand,
  type DiffReviewCommand,
  FIND_IN_CHANGES_LABEL,
} from "./diffReviewCommands";
import { useOwnedFilesOnly, writeOwnedFilesOnly } from "./ownedFilesPreference";
import { useDiffWorkers } from "./sharedDiffWorkers";
import {
  type DiffReviewBase,
  DiffReviewToolbar,
  type DiffReviewToolbarProps,
} from "./DiffReviewToolbar";
import { type DiffReviewPresentation, onlyWhitespaceChanged } from "./reviewPresentation";
import { DiffReviewTree, OWNED_BY_YOU } from "./DiffReviewTree";
import { useFocusedReviewFolder } from "./focusedReviewFolder";
import {
  applyFilterAction,
  DEFAULT_DIFF_REVIEW_FILTERS,
  effectiveReviewFilters,
  filterReviewPaths,
  hasActiveFilters,
  hasTreeSearch,
  languageCounts,
  lineBreakdown,
  ownedFileCount,
  pathFromReviewItemId,
  reviewItemId,
  syntaxLanguage,
  treeOrderedFiles,
  visibleReviewFiles,
  type DiffFilterAction,
  type DiffReviewFile,
  type DiffReviewFilters,
} from "./diffReviewModel";
import { MarkdownDiffPreview } from "./MarkdownDiffPreview";
import { MARKDOWN_DIFF_SECTION_ATTRIBUTE } from "./markdownDiffSection";
import { characterTop, DIFF_FILE_HEADER_HEIGHT, readPreviewAnchor } from "./previewReadingPoint";
import {
  createPreviewWidthAnchor,
  isForeignScroll,
  type OwnScrollWrite,
} from "./previewWidthAnchor";
import {
  clearOwnedFindHighlights,
  DIFF_FIND_HIGHLIGHT_CSS,
  DIFF_FIND_HIGHLIGHTS,
  firstRenderedMatchAtOrAfter,
  paintCurrentMatch,
  searchMatchRange,
} from "./findHighlight";
import {
  findQueryFromSelection,
  type ReviewSearchLine,
  type ReviewSearchMatch,
} from "./findMatches";
import {
  buildReviewItems,
  fileLineCounts,
  isExpandable,
  placeholderLabel,
  reviewSearchLines,
  type DiffReviewContent,
} from "./reviewItems";
import {
  createAlignment,
  fileAnchorOffset,
  switchSettled,
  type AlignOutcome,
} from "./fileScrollAnchor";
import { FILE_HEADER_FLASH_CSS, flashFileHeader } from "./fileHeaderFlash";
import { containsAcrossShadowRoots, currentSideRow, revealColumn, sideRow } from "./renderedRows";
import {
  currentLineForOldLine,
  hunksHaveLine,
  rangeFits,
  readScrollAnchor,
  type ReviewRevealTarget,
  type ReviewScrollAnchor,
} from "./reviewNavigation";
import {
  reviewCanSplit,
  reviewShowsTreeColumn,
  reviewToolbarIsCompact,
  useElementWidth,
} from "./reviewLayout";
import { PULL_REQUEST_REVIEW_TREE_WIDTH } from "./reviewTreeWidth";
import { readDiffReviewTreeVisible, writeDiffReviewTreeVisible } from "./treeVisibility";
import {
  clearViewed,
  markViewed,
  useViewedMarks,
  viewedFingerprint,
  viewedBatch,
  viewedUnavailableReason,
  viewedMarkCovers,
} from "./viewedFiles";
import { useDiffReviewFind } from "./useDiffReviewFind";
import { CONTEXT_LOAD_CSS, type ContextLoadState, markContextLoads } from "./contextLoadIndicator";
import { ContextLoadStatus } from "./ContextLoadStatus";
import { useContextLoads } from "./useContextLoads";
import {
  createDiffRenderFailureReporter,
  DIFF_RENDER_FAILED_MESSAGE,
  type DiffReviewView,
} from "./diffRenderFailure";
import {
  canPreviewMarkdown,
  isMarkdownPreviewAnnotation,
  markdownDiffViewAction,
  shownRichPaths,
  useMarkdownPreviews,
  withMarkdownPreviewItems,
  withMarkdownPreviewSearchLines,
  type MarkdownDiffViewAction,
  type MarkdownPreviewItemCache,
  type MarkdownPreviews,
} from "./useMarkdownPreviews";
import {
  useReviewTextLayer,
  type ReviewTextEvent,
  type ReviewTextHighlight,
} from "./useReviewTextLayer";

/** How long to wait for a file to render before revealing a line in it anyway. */
const REVEAL_RENDER_BUDGET_MS = 3000;

/** How long a reveal waits for a patch's full text before scrolling without it. */
const FULL_TEXT_BUDGET_MS = 15000;

/** How long the view is still before the reader's place is read again, under what covers it. */
const SETTLED_CAPTURE_MS = 200;

/** How long a preview goes without resizing before a width change's restore lets go. */
const PREVIEW_SETTLE_MS = 150;

/** How many times a scroll corrects for layout that is still settling. */
const MAX_ALIGN_CORRECTIONS = 4;

/** Run `next` after CodeView has laid out the current change. */
function afterLayout(next: () => void) {
  requestAnimationFrame(() => requestAnimationFrame(next));
}

/** The line under the pointer in the gutter, on the side it's on. */
export type DiffReviewHoveredLine = () => { lineNumber: number; side: AnnotationSide } | undefined;

/** Where a pointer gesture over line numbers is: still moving, or let go. */
export type DiffReviewSelectionPhase = "preview" | "commit";

/**
 * What one source adds to the shared review, such as a pull request's review
 * conversations. Every part is optional; the diff renders the same without it.
 */
export interface DiffReviewSourceLayer<A> {
  /** The names find paints its highlights under; a pull request's review uses its own. */
  findHighlightNames?: SearchHighlightNames;
  /** Rows shown under diff lines, by path. Line 0 shows above the file's first hunk. */
  annotations?: ReadonlyMap<string, readonly DiffLineAnnotation<A>[]>;
  renderAnnotation?: (annotation: DiffLineAnnotation<A>, path: string) => ReactNode;
  /** A control beside the hovered line's number, such as starting a conversation there. */
  renderGutterUtility?: (hoveredLine: DiffReviewHoveredLine, path: string) => ReactNode;
  /** Lines the source selects on either side; while set, it replaces `markedLines`. */
  selectedLines?: { path: string; range: SelectedLineRange } | null;
  /** Line selections made with the pointer over line numbers. */
  onLineSelection?: (
    path: string,
    range: SelectedLineRange | null,
    phase: DiffReviewSelectionPhase,
  ) => void;
  /** Loads a file's full text when the reader expands unchanged lines its diff leaves out. */
  expandContext?: { load: FileDiffContentsLoader; lineCount: number };
  /** Shown in a file's header before its actions menu, such as a conversation count. */
  renderHeaderExtras?: (file: DiffReviewFile) => ReactNode;
  /** Controls at the toolbar's end, after the review's own. */
  toolbar?: ReactNode;
  /** Progress left of the line counts, such as threads still loading; nothing after it moves. */
  toolbarStatus?: ReactNode;
  /** Shown after the last file, such as conversations no file shows. */
  footer?: ReactNode;
}

export interface DiffReviewProps<A = undefined> {
  /** Which review this is, for the desktop diagnostics log. */
  diagnosticsView: DiffReviewView;
  files: readonly DiffReviewFile[];
  /** What the files are compared against, named beside the file count. */
  base?: DiffReviewBase | null;
  contents: ReadonlyMap<string, DiffReviewContent>;
  onRetry: (path: string) => void;
  /** The file the person opened; the review scrolls to it whenever it changes. */
  revealPath?: string | null;
  layout: "unified" | "split";
  /** Offers the unified/split choice in the toolbar and ⌘K. */
  onLayoutChange?: (layout: "unified" | "split") => void;
  wrapLines: boolean;
  /** Offers line wrapping in the toolbar and ⌘K. */
  onWrapLinesChange?: (wrap: boolean) => void;
  /** Whether the source left whitespace-only changes out of the diffs. */
  ignoreWhitespace?: boolean;
  /** Offers ignoring whitespace in the toolbar and ⌘K, for sources that can recompute diffs. */
  onIgnoreWhitespaceChange?: (ignore: boolean) => void;
  /** The ⌘K heading for this review's commands, naming what it reviews. */
  commandHeading?: string;
  /** Whether ⌘K offers this review's commands; false while the review is mounted but hidden. */
  commandsActive?: boolean;
  findOpen: boolean;
  /** Bumped on every find request so the query is focused again. */
  findFocusRequest: number;
  onFindClose: () => void;
  /** Shown when the source has no changed files. */
  emptyMessage: string;
  /** Ranges of current text to highlight, such as comments. */
  highlights?: readonly ReviewTextHighlight[];
  /** Selections and clicks on current text, as offsets into it. */
  onTextEvent?: (event: ReviewTextEvent) => void;
  /** A line to bring into view; each new object is one request. */
  revealTarget?: ReviewRevealTarget | null;
  /** Current-version lines to mark, such as a cited range. */
  markedLines?: { path: string; start: number; end: number } | null;
  /** Where to put the reader when the review opens, instead of the opened file's top. */
  initialScrollAnchor?: ReviewScrollAnchor;
  onScrollAnchorChange?: (anchor: ReviewScrollAnchor) => void;
  /**
   * Per-file actions for each file header's menu; none hides the menu. ⌘K
   * offers the current file's under "File".
   */
  fileActions?: (file: DiffReviewFile) => readonly DiffReviewFileAction[];
  /** A changed-files tree folder's actions after its Collapse or Expand, such as copying its path. */
  folderActions?: (path: string) => readonly DiffReviewFileAction[];
  /** Whether ⌘K offers the current file's actions; false while another file, such as a focused row, has them. */
  fileActionsInPalette?: boolean;
  /**
   * Opens a file in a tab of its own, behind this one unless `background` is false.
   * With it, file menus, the changed-files tree's middle click and ⌘Enter, and ⌘K offer it.
   */
  onOpenFileInNewTab?: (path: string, options: { background: boolean }) => void;
  /** A file the reader picked in the changed-files tree, after the review scrolls to it. */
  onTreeFileChosen?: (path: string) => void;
  /** The source's own layer: annotations, line actions, and extra chrome. */
  source?: DiffReviewSourceLayer<A>;
  /**
   * Names this review, such as a session's changes or one pull request, so each
   * file's rich or source choice and viewed mark are remembered.
   */
  reviewScope?: string;
  /**
   * False for a review whose files can't be marked viewed, such as a comparison shown only to
   * read: no marks are read or written, and no viewed toggle or command is offered.
   */
  viewedMarks?: boolean;
  /** Another design's chrome, such as Changes review's; absent renders today's review. */
  presentation?: DiffReviewPresentation;
}

const SHADOW_CSS = `
  [data-line] { user-select: text; }
  [data-header-content] { gap: 8px; }
  /* The review draws the file's name itself, its folder quieter than its name. */
  [data-header-content] > [data-title] { display: none; }
  /* Let the title take the header's free space and shrink before the controls beside it. */
  [data-header-content] { flex: 1 1 auto; }
  ::slotted([slot="header-filename-suffix"]) { flex: 1 1 auto; min-width: 0; }
  /* A renamed file's old path gives way before its new name in a narrow header. */
  [data-header-content] > [data-prev-name] { flex-shrink: 1000; min-width: 2.5rem; }
  ::slotted([slot="header-metadata"]) { flex: none; }
  /* A narrow rail keeps room for the name: the toolbar totals the line counts, and
     the title's copy button gives way to the same action in the ⋯ menu. */
  [data-diffs-header] { container-type: inline-size; }
  @container (max-width: 360px) {
    [data-deletions-count], [data-additions-count] { display: none; }
  }
  /* Below that, a renamed file shows only its new name; its icon still marks the rename. */
  @container (max-width: 280px) {
    [data-header-content] > [data-prev-name], [data-header-content] > [data-rename-icon] { display: none; }
  }
  /* A rich Markdown diff is an empty file whose line-0 row holds the preview: show only that row. */
  pre[data-file]:has(slot[name="annotation-0"]) [data-gutter],
  pre[data-file]:has(slot[name="annotation-0"]) [data-content] > [data-line] { display: none; }
  ${DIFF_PALETTE_CSS}
  ${DIFF_FIND_HIGHLIGHT_CSS}
  ${CONTEXT_LOAD_CSS}
  ${FILE_HEADER_FLASH_CSS}
`;

const NO_HIGHLIGHTS: readonly ReviewTextHighlight[] = [];
const NO_COMMANDS: PaletteCommand[] = [];
const NO_VIEWED_MARKS: ReadonlyMap<string, string> = new Map();
const NO_SEARCH_LINES: readonly ReviewSearchLine[] = [];

/** Why "Mark all files as viewed" waits: a mark records each file's loaded changes. */
export const MARK_ALL_VIEWED_LOADING_REASON =
  "You can mark every file viewed once their diffs load";

function useCodeFont() {
  return useSyncExternalStore(
    (listener) => subscribeCodeFont(() => listener()),
    () => {
      const font = readCodeFont();
      return `${font.sizePx}|${font.family}|${font.weight}`;
    },
  );
}

export function DiffReview<A = undefined>({
  diagnosticsView,
  files: givenFiles,
  base,
  contents: sourceContents,
  onRetry,
  revealPath,
  layout,
  onLayoutChange,
  wrapLines,
  onWrapLinesChange,
  ignoreWhitespace = false,
  onIgnoreWhitespaceChange,
  commandHeading = "Diff review",
  commandsActive = true,
  findOpen,
  findFocusRequest,
  onFindClose,
  emptyMessage,
  highlights = NO_HIGHLIGHTS,
  onTextEvent,
  revealTarget,
  markedLines,
  initialScrollAnchor,
  onScrollAnchorChange,
  fileActions,
  folderActions,
  fileActionsInPalette = true,
  onOpenFileInNewTab,
  onTreeFileChosen,
  source,
  reviewScope = "review",
  viewedMarks: viewedMarksOffered = true,
  presentation,
}: DiffReviewProps<A>) {
  // The diff lists its files in the tree's order, so the two read the same top to bottom.
  const files = useMemo(() => treeOrderedFiles(givenFiles), [givenFiles]);
  const viewer = useRef<CodeViewHandle<A>>(null);
  const [shell, setShell] = useState<HTMLDivElement | null>(null);
  const width = useElementWidth(shell);
  // An unmeasured width (pre-layout, tests) counts as wide.
  const wide = reviewShowsTreeColumn(width);
  const canSplit = reviewCanSplit(width);
  const shownLayout = canSplit ? layout : "unified";
  const [treeOpen, setTreeOpen] = useState(() => readDiffReviewTreeVisible(true));
  const [narrowTreeOpen, setNarrowTreeOpen] = useState(false);
  const [treeSearch, setTreeSearch] = useState<string | null>(null);
  const [treeSearchFocusRequest, setTreeSearchFocusRequest] = useState(0);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [languagesOpen, setLanguagesOpen] = useState(false);
  const [chosenFilters, setFilters] = useState<DiffReviewFilters>(DEFAULT_DIFF_REVIEW_FILTERS);
  // The owned-only choice is remembered per review; it applies while the reader owns a file.
  const chosenOwnedOnly = useOwnedFilesOnly(reviewScope);
  const filters = useMemo(
    () => effectiveReviewFilters(files, { ...chosenFilters, ownedFilesOnly: chosenOwnedOnly }),
    [files, chosenFilters, chosenOwnedOnly],
  );
  const [collapsed, setCollapsed] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const themeType = useResolvedThemeMode();
  const codeFont = useCodeFont();
  const [codeFontSize, fontFamily, fontWeight] = codeFont.split("|");
  const metrics = presentation?.codeMetrics;
  const fontSize = metrics ? String(metrics.fontSizePx) : codeFontSize;
  const lineHeight = metrics ? metrics.lineHeightPx : Math.round(Number(fontSize) * 1.5);
  const bodyRef = useRef<HTMLDivElement>(null);
  const treeDocked = wide && treeOpen && files.length > 0;
  // The tree keeps its share of the review while it's docked beside the diff.
  const treeColumn = useRememberedPaneWidth(
    presentation?.treeColumn ?? PULL_REQUEST_REVIEW_TREE_WIDTH,
    bodyRef,
    treeDocked ? width : null,
  );

  useEffect(() => writeDiffReviewTreeVisible(treeOpen), [treeOpen]);

  // A patch's diff leaves out unchanged lines. Once its full text loads, for an
  // expansion or a reveal, the review shows, searches, and reveals the full diff.
  // Diffs the library couldn't draw, by path; each file's note replaces its diff.
  const [renderFailures, setRenderFailures] = useState<ReadonlyMap<string, FileDiffMetadata>>(
    () => new Map(),
  );
  const latestView = useRef(diagnosticsView);
  latestView.current = diagnosticsView;
  const [reportRenderFailure] = useState(() =>
    createDiffRenderFailureReporter(() => latestView.current),
  );
  const onRenderError = useCallback(
    (error: Error, fileDiff: FileDiffMetadata | undefined, context: { item: { id: string } }) => {
      const path = pathFromReviewItemId(context.item.id);
      reportRenderFailure(path, fileDiff, error);
      if (!fileDiff) return;
      setRenderFailures((failures) =>
        failures.get(path) === fileDiff ? failures : new Map(failures).set(path, fileDiff),
      );
    },
    [reportRenderFailure],
  );
  const contextLoads = useContextLoads(source?.expandContext?.load);
  const loadFullText = contextLoads.load;
  const latestLoadFullText = useRef(loadFullText);
  latestLoadFullText.current = loadFullText;
  const {
    hydrated: hydratedDiff,
    states: contextLoadStates,
    retry: retryContextLoad,
  } = contextLoads;
  const contents = useMemo(() => {
    let merged: Map<string, DiffReviewContent> | undefined;
    for (const [path, content] of sourceContents) {
      if (content.status !== "ready") continue;
      const loaded = hydratedDiff(content.fileDiff);
      if (!loaded) continue;
      merged ??= new Map(sourceContents);
      merged.set(path, { ...content, fileDiff: loaded });
    }
    // A diff the library couldn't draw shows its note instead, until the file has a new diff.
    for (const [path, failed] of renderFailures) {
      const content = (merged ?? sourceContents).get(path);
      if (content?.status !== "ready" || content.fileDiff !== failed) continue;
      merged ??= new Map(sourceContents);
      merged.set(path, { status: "unavailable", message: DIFF_RENDER_FAILED_MESSAGE });
    }
    return merged ?? sourceContents;
  }, [sourceContents, hydratedDiff, renderFailures]);
  // Each file's load state by its path, for its separators and header.
  const contextLoadsByPath = useMemo(() => {
    const byPath = new Map<string, { state: ContextLoadState; fileDiff: FileDiffMetadata }>();
    if (contextLoadStates.size === 0) return byPath;
    for (const [path, content] of sourceContents) {
      if (content.status !== "ready") continue;
      const state = contextLoadStates.get(content.fileDiff);
      if (state) byPath.set(path, { state, fileDiff: content.fileDiff });
    }
    return byPath;
  }, [contextLoadStates, sourceContents]);
  const latestContextLoads = useRef(contextLoadsByPath);
  latestContextLoads.current = contextLoadsByPath;
  const markRenderedContextLoads = useCallback(() => {
    markContextLoads(
      viewer.current?.getInstance()?.getRenderedItems() ?? [],
      (itemId) => latestContextLoads.current.get(pathFromReviewItemId(itemId))?.state,
    );
  }, []);
  useEffect(() => markRenderedContextLoads(), [contextLoadsByPath, markRenderedContextLoads]);

  // The opened file stays listed when filters hide it, until the person changes a filter.
  const [pinnedPath, setPinnedPath] = useState(revealPath ?? null);
  const [pinnedFor, setPinnedFor] = useState(revealPath ?? null);
  if (pinnedFor !== (revealPath ?? null)) {
    setPinnedFor(revealPath ?? null);
    setPinnedPath(revealPath ?? null);
  }
  const shownFiles = useMemo(
    () => visibleReviewFiles(files, filters, pinnedPath ?? undefined),
    [files, filters, pinnedPath],
  );
  const shownPaths = useMemo(() => shownFiles.map((file) => file.path), [shownFiles]);
  const searchedPaths = useMemo(
    () => new Set(filterReviewPaths(shownPaths, treeSearch)),
    [shownPaths, treeSearch],
  );
  const listedFiles = useMemo(
    () => shownFiles.filter((file) => searchedPaths.has(file.path)),
    [shownFiles, searchedPaths],
  );
  // The breakdown closes for good when its review is hidden or lists nothing, so it never
  // lingers over another view or reopens by itself once files return.
  const languagesAvailable = commandsActive && listedFiles.length > 0;
  if (languagesOpen && !languagesAvailable) setLanguagesOpen(false);
  const markdown = useMarkdownPreviews(reviewScope, loadFullText);
  const shownRich = useMemo(
    () => shownRichPaths(files, contents, markdown.richPaths),
    [files, contents, markdown.richPaths],
  );
  // The rich files the diff view last rendered: set after CodeView's own layout effect has
  // taken the items this render built from `shownRich`.
  const committedRich = useRef(shownRich);
  useLayoutEffect(() => {
    committedRich.current = shownRich;
  });
  // A viewed file starts collapsed, until its changes differ from the ones marked.
  const storedViewedMarks = useViewedMarks(reviewScope);
  const viewedMarks = viewedMarksOffered ? storedViewedMarks : NO_VIEWED_MARKS;
  const filesByPath = useMemo(() => new Map(files.map((file) => [file.path, file])), [files]);
  const viewedPaths = useMemo(() => {
    const viewed = new Set<string>();
    // Only marked files are fingerprinted, so a review nobody marked hashes nothing.
    for (const [path, mark] of viewedMarks) {
      const file = filesByPath.get(path);
      if (file && viewedMarkCovers(mark, viewedFingerprint(file, contents.get(path)))) {
        viewed.add(path);
      }
    }
    return viewed;
  }, [filesByPath, contents, viewedMarks]);
  const sourceAnnotations = source?.annotations;
  const [previewItemCache] = useState<MarkdownPreviewItemCache>(() => new Map());
  const items = useMemo(
    () =>
      withMarkdownPreviewItems(
        buildReviewItems<A>({
          files: listedFiles,
          contents,
          collapsed,
          viewed: viewedPaths,
          annotations: sourceAnnotations,
        }),
        shownRich,
        markdown.states,
        previewItemCache,
      ),
    [
      listedFiles,
      contents,
      collapsed,
      viewedPaths,
      sourceAnnotations,
      shownRich,
      markdown.states,
      previewItemCache,
    ],
  );
  const searchLines = useMemo(
    () =>
      withMarkdownPreviewSearchLines(
        reviewSearchLines(listedFiles, contents),
        shownRich,
        markdown.states,
      ),
    [listedFiles, contents, shownRich, markdown.states],
  );
  // The files the filters and path search leave out, searched the same way only while find
  // is open, so find can say how many matches they hold. A linked file shown anyway isn't one.
  const hiddenSearchLines = useMemo(() => {
    if (!findOpen) return NO_SEARCH_LINES;
    const listed = new Set(listedFiles.map((file) => file.path));
    const hidden = files.filter((file) => !listed.has(file.path));
    if (hidden.length === 0) return NO_SEARCH_LINES;
    return withMarkdownPreviewSearchLines(
      reviewSearchLines(hidden, contents),
      shownRich,
      markdown.states,
    );
  }, [findOpen, files, listedFiles, contents, shownRich, markdown.states]);
  // Find searches rich files' rendered text, so it prepares them even off screen.
  const requestPreview = markdown.request;
  useEffect(() => {
    if (!findOpen) return;
    for (const path of shownRich) {
      const content = contents.get(path);
      if (content?.status === "ready") requestPreview(path, content.fileDiff);
    }
  }, [findOpen, shownRich, contents, requestPreview]);
  // The toolbar's totals and its by-language breakdown come from one pass over the same counts.
  const breakdown = useMemo(
    () => lineBreakdown(listedFiles, (file) => fileLineCounts(file, contents.get(file.path))),
    [listedFiles, contents],
  );
  const expandablePaths = useMemo(
    () =>
      listedFiles
        .filter((file) => isExpandable(file, contents.get(file.path)))
        .map((file) => file.path),
    [listedFiles, contents],
  );
  // A set, so checking every item stays linear in a review of many collapsed files.
  const allCollapsed = useMemo(() => {
    if (expandablePaths.length === 0) return false;
    const expandable = new Set(expandablePaths);
    return items.every((item) => !expandable.has(pathFromReviewItemId(item.id)) || item.collapsed);
  }, [expandablePaths, items]);

  const dispatchFilter = useCallback(
    (action: DiffFilterAction) => {
      setPinnedPath(null);
      if (action.type === "setOwnedFilesOnly") writeOwnedFilesOnly(reviewScope, action.value);
      if (action.type === "clearFilters") writeOwnedFilesOnly(reviewScope, false);
      setFilters((current) => applyFilterAction(current, action, files));
    },
    [files, reviewScope],
  );
  const ownedCount = ownedFileCount(files);
  const ownedFiles = useMemo(
    () =>
      ownedCount > 0
        ? {
            only: filters.ownedFilesOnly,
            count: ownedCount,
            setOnly: (value: boolean) => dispatchFilter({ type: "setOwnedFilesOnly", value }),
          }
        : undefined,
    [ownedCount, filters.ownedFilesOnly, dispatchFilter],
  );

  const setFileExpanded = useCallback((path: string, expanded: boolean) => {
    setCollapsed((current) => new Map(current).set(path, !expanded));
  }, []);

  // Drops the reader's own fold on `paths`, so the viewed default decides again.
  const forgetCollapsed = useCallback((paths: readonly string[]) => {
    setCollapsed((current) => {
      if (!paths.some((path) => current.has(path))) return current;
      const next = new Map(current);
      for (const path of paths) next.delete(path);
      return next;
    });
  }, []);

  // Marking a file viewed folds it away and clearing the mark opens it again,
  // through the viewed default, so changes made after the mark open it too.
  const toggleFileViewed = useCallback(
    (file: DiffReviewFile) => {
      if (viewedPaths.has(file.path)) {
        clearViewed(reviewScope, file.path);
      } else {
        // A file still loading has no revision to mark; the header and ⌘K wait for it.
        const fingerprint = viewedFingerprint(file, contents.get(file.path));
        if (fingerprint === null) return;
        markViewed(reviewScope, file.path, fingerprint);
      }
      forgetCollapsed([file.path]);
    },
    [viewedPaths, reviewScope, contents, forgetCollapsed],
  );

  // A mark records a revision, so a file can be marked only once it has one; null when it can.
  const viewedBlockedReason = useCallback(
    (file: DiffReviewFile) =>
      viewedPaths.has(file.path) ? null : viewedUnavailableReason(file, contents.get(file.path)),
    [viewedPaths, contents],
  );

  const toggleAllFiles = useCallback(() => {
    setCollapsed(new Map(expandablePaths.map((path) => [path, !allCollapsed])));
  }, [expandablePaths, allCollapsed]);

  // Opening a file scrolls the review to it once it's listed, expanding it if
  // collapsed, unless the review restores the reader's place, a requested line
  // takes over, or the file became current through the reader's own action here.
  const handledReveal = useRef<string | null>(null);
  // The file the review opens on, as after a reload, keeps a viewed fold; opening it later reveals it.
  const openingRevealPath = useRef(revealPath ?? null);
  const pendingAnchor = useRef(initialScrollAnchor);
  // Where the pending anchor came from: a saved place the review opened on, where the reader
  // scrolled to while diffs above it were loading, or a file they picked meanwhile.
  const anchorSource = useRef<"saved" | "reader" | "file">("saved");
  // Whether a file up to `path` is still loading: its diff arriving moves what's at `path`.
  const loadingThrough = useRef<(path: string) => boolean>(() => false);
  loadingThrough.current = (path) => {
    const index = listedFiles.findIndex((file) => file.path === path);
    return listedFiles
      .slice(0, index + 1)
      .some((file) => !file.isBinary && contents.get(file.path)?.status === "loading");
  };
  const focusedHere = useRef<string | null>(null);

  // Programmatic placement yields to the reader: input on the review cancels
  // pending reveals and restores, and a newer placement supersedes an older one.
  const placement = useRef(0);
  const latestRevealPath = useRef(revealPath);
  latestRevealPath.current = revealPath;
  const latestRevealTarget = useRef(revealTarget);
  latestRevealTarget.current = revealTarget;
  const abandonedTarget = useRef<ReviewRevealTarget | null>(null);
  // A rich diff's reader stays at their place when its preview's width changes.
  const widthAnchor = useRef(createPreviewWidthAnchor());
  /**
   * A new programmatic placement. Any other than a width change's own restore is a new intent, so
   * the reading place saved for a width change is forgotten at once.
   */
  const startPlacement = useCallback((options: { keepsWidthAnchor?: boolean } = {}) => {
    const generation = ++placement.current;
    if (!options.keepsWidthAnchor) widthAnchor.current.reset();
    return () => placement.current === generation;
  }, []);
  useEffect(() => {
    if (!shell) return;
    const takeOver = () => {
      placement.current += 1;
      widthAnchor.current.reset();
      pendingAnchor.current = undefined;
      // The opened file isn't scrolled to later either.
      if (latestRevealPath.current) handledReveal.current = latestRevealPath.current;
      abandonedTarget.current = latestRevealTarget.current ?? null;
    };
    const events = ["wheel", "touchstart", "pointerdown", "keydown"] as const;
    for (const event of events) {
      shell.addEventListener(event, takeOver, { passive: true, capture: true });
    }
    return () => {
      for (const event of events) shell.removeEventListener(event, takeOver, { capture: true });
    };
  }, [shell]);

  // CodeView places items by estimated heights until they render, so a scroll
  // lands near its target; correct by where the target actually is each time
  // CodeView renders, while layout above it is still shifting. With `hold`, keep
  // correcting until the target itself is done changing, however often it moves.
  const postRenderListeners = useRef(new Set<() => void>());
  /**
   * Keep the position `resolve` finds (a viewport top) `offset` from the view's top while layout
   * settles: at most a few corrections within the render budget, or held with `hold`. `done`
   * hears, exactly once, whether it ended in place, ran out of corrections short of it, or gave
   * way (to the reader, a newer placement, the deadline, or a target that went away).
   */
  const alignTarget = useCallback(
    (
      resolve: (
        instance: NonNullable<ReturnType<CodeViewHandle<A>["getInstance"]>>,
      ) => number | undefined,
      offset: number,
      isCurrent: () => boolean,
      hold?: { settled: () => boolean; budgetMs: number },
      done?: (outcome: AlignOutcome) => void,
    ) => {
      const deadline = performance.now() + (hold?.budgetMs ?? REVEAL_RENDER_BUDGET_MS);
      const attempt = createAlignment({
        offset,
        corrections: hold ? Number.POSITIVE_INFINITY : MAX_ALIGN_CORRECTIONS,
        settled: hold?.settled,
        read: () => {
          const instance = viewer.current?.getInstance();
          const container = instance?.getContainerElement();
          if (!isCurrent() || performance.now() > deadline || !instance || !container) {
            return "abandoned";
          }
          const targetTop = resolve(instance);
          if (targetTop === undefined) return "pending";
          return { targetTop, viewTop: container.getBoundingClientRect().top };
        },
        // A native scroll, like the reader's own: CodeView's position targets are
        // in its logical coordinates, which can differ from the element's offset.
        scrollBy: (top) => {
          const instance = viewer.current?.getInstance();
          const container = instance?.getContainerElement();
          if (!instance || !container) return;
          const before = Math.round(container.scrollTop);
          container.scrollBy({ top, behavior: "instant" });
          const after = Math.round(container.scrollTop);
          // Only a write that moved the view owns where it landed; a no-op claims nothing.
          if (after !== before) ownScrollWrite.current = { instance, top: after };
        },
        afterLayout,
        done: (outcome) => {
          postRenderListeners.current.delete(attempt);
          done?.(outcome);
        },
      });
      postRenderListeners.current.add(attempt);
      afterLayout(attempt);
    },
    [],
  );
  const alignRendered = useCallback(
    (
      id: string,
      line: number | undefined,
      offset: number,
      isCurrent: () => boolean,
      hold?: { settled: () => boolean; budgetMs: number },
    ) =>
      alignTarget(
        (instance) => {
          const item = instance.getRenderedItems().find((rendered) => rendered.id === id);
          const root = item?.element.shadowRoot;
          const target =
            line === undefined ? item?.element : root ? currentSideRow(root, line) : undefined;
          return target?.getBoundingClientRect().top;
        },
        offset,
        isCurrent,
        hold,
      ),
    [alignTarget],
  );

  const capturePending = useRef(false);
  const settledCapture = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(settledCapture.current), []);
  /**
   * Record where the reader is once the view has laid out, and again once it has been still a
   * moment: a file's sticky header is drawn a few frames after a jump and covers the view's
   * top. Right after a jump, the group there may still be skipped and can't be hit-tested, so
   * a miss looks again, at most twice.
   */
  const captureReadingAnchor = useCallback((container: HTMLElement, retries = 2) => {
    clearTimeout(settledCapture.current);
    settledCapture.current = setTimeout(
      () => widthAnchor.current.capture(readPreviewAnchor(container, DIFF_FILE_HEADER_HEIGHT)),
      SETTLED_CAPTURE_MS,
    );
    if (capturePending.current) return;
    capturePending.current = true;
    afterLayout(() => {
      capturePending.current = false;
      const reading = readPreviewAnchor(container, DIFF_FILE_HEADER_HEIGHT);
      if (!reading && retries > 0) {
        captureReadingAnchor(container, retries - 1);
        return;
      }
      widthAnchor.current.capture(reading);
    });
  }, []);
  const previewResizedAt = useRef(0);
  // When a placement last scrolled the view itself.
  // Where the width restore's own last scroll left the view, until the scroll it caused.
  const ownScrollWrite = useRef<OwnScrollWrite | null>(null);
  // Lets go of the width restore under way, if one is.
  const yieldWidthRestore = useRef<(() => void) | null>(null);
  const onPreviewResize = useCallback(
    (path: string, previewWidth: number, version: string | null) => {
      previewResizedAt.current = performance.now();
      const restore = widthAnchor.current.widthChanged(path, version, previewWidth);
      if (!restore) {
        // Its height changed, as when a group lays out at its real size, so what's at the
        // view's top may be too, without a scroll to say so.
        const container = viewer.current?.getInstance()?.getContainerElement();
        if (container) captureReadingAnchor(container);
        return;
      }
      const { anchor: target, token } = restore;
      const anchor = widthAnchor.current;
      const byText = target.text;
      const placementCurrent = startPlacement({ keepsWidthAnchor: true });
      let yielded = false;
      const release = () => {
        yielded = true;
      };
      yieldWidthRestore.current = release;
      alignTarget(
        (instance) => {
          const section = instance
            .getContainerElement()
            ?.querySelector(
              `[data-markdown-diff-path="${CSS.escape(target.path)}"] ` +
                `[data-markdown-diff-version="${CSS.escape(target.version)}"] ` +
                `[${MARKDOWN_DIFF_SECTION_ATTRIBUTE}="${target.section}"]`,
            );
          if (!section) return undefined;
          // The reader's character when it can still be found, else their section's top.
          const characterAt = byText ? characterTop(section, byText.offset) : null;
          if (characterAt !== null) return characterAt;
          // Aligned to the character's offset, so shifted to put the section's top at its own.
          return section.getBoundingClientRect().top + (byText ? byText.top - target.offset : 0);
        },
        byText ? byText.top : target.offset,
        () => !yielded && placementCurrent(),
        // Held while the preview keeps resizing: the diff view corrects its own scroll for the
        // new height a few frames later, which would carry the reader's place away.
        {
          settled: () => performance.now() - previewResizedAt.current > PREVIEW_SETTLE_MS,
          budgetMs: REVEAL_RENDER_BUDGET_MS,
        },
        (outcome) => {
          if (yieldWidthRestore.current === release) yieldWidthRestore.current = null;
          return outcome === "abandoned" ? anchor.abandoned(token) : anchor.restored(token);
        },
      );
    },
    [alignTarget, captureReadingAnchor, startPlacement],
  );
  // One check per frame however many items a render pass touched, outside CodeView's render.
  const renderNotice = useRef<number | undefined>(undefined);
  const notifyRendered = useCallback(() => {
    if (postRenderListeners.current.size === 0 || renderNotice.current !== undefined) return;
    renderNotice.current = requestAnimationFrame(() => {
      renderNotice.current = undefined;
      for (const listener of [...postRenderListeners.current]) listener();
    });
  }, []);
  useEffect(
    () => () => {
      if (renderNotice.current !== undefined) cancelAnimationFrame(renderNotice.current);
    },
    [],
  );

  const scrollToFile = useCallback(
    (path: string) => {
      const id = reviewItemId(path);
      viewer.current?.scrollTo({ type: "item", id, align: "start", behavior: "instant" });
      alignRendered(id, undefined, 0, startPlacement());
      // Opened while diffs above it are loading: put it back at the top once they've grown.
      if (loadingThrough.current(path)) {
        pendingAnchor.current = { path, offset: 0 };
        anchorSource.current = "file";
      }
    },
    [alignRendered, startPlacement],
  );

  // In the phone layout the narrow tree spans the screen over the diff, so a pick closes it;
  // anywhere else it's an anchored popover that stays open for the next pick.
  const treePopoverFullWidth = useIsMobileViewport();
  // A tree pick flashes the file's header once it's rendered at its new place.
  const flashGeneration = useRef(0);
  const flashFileWhenRendered = useCallback((path: string) => {
    const id = reviewItemId(path);
    const generation = ++flashGeneration.current;
    const attempt = (remaining: number) => {
      if (generation !== flashGeneration.current) return;
      const rendered = viewer.current
        ?.getInstance()
        ?.getRenderedItems()
        .find((item) => item.id === id)?.element;
      if (rendered && flashFileHeader(rendered)) return;
      if (remaining > 0) requestAnimationFrame(() => attempt(remaining - 1));
    };
    requestAnimationFrame(() => attempt(30));
  }, []);
  const navigateToFile = useCallback(
    (path: string) => {
      scrollToFile(path);
      flashFileWhenRendered(path);
      if (treePopoverFullWidth) setNarrowTreeOpen(false);
    },
    [scrollToFile, flashFileWhenRendered, treePopoverFullWidth],
  );

  useEffect(() => {
    if (openingRevealPath.current !== (revealPath ?? null)) openingRevealPath.current = null;
    // No file requested re-arms the reveal, so asking for the same file again scrolls to it.
    if (!revealPath) {
      handledReveal.current = null;
      return;
    }
    if (handledReveal.current === revealPath) return;
    if (focusedHere.current === revealPath || revealTarget?.path === revealPath) {
      focusedHere.current = null;
      handledReveal.current = revealPath;
      pendingAnchor.current = undefined;
      return;
    }
    // A file opened after the reader scrolled or picked one is the newer intent. A saved place
    // waits only for the file the review opened on; a file asked for since, by Back or a link, wins.
    if (pendingAnchor.current && anchorSource.current === "saved") {
      if (openingRevealPath.current === revealPath) return;
      pendingAnchor.current = undefined;
    }
    if (!filesByPath.has(revealPath)) return;
    pendingAnchor.current = undefined;
    if (!searchedPaths.has(revealPath)) {
      setTreeSearch(null);
      return;
    }
    const item = items.find((candidate) => candidate.id === reviewItemId(revealPath));
    if (!item) return;
    const file = filesByPath.get(revealPath)!;
    if (
      item.collapsed &&
      isExpandable(file, contents.get(revealPath)) &&
      !collapsed.has(revealPath) &&
      !(openingRevealPath.current === revealPath && viewedPaths.has(revealPath))
    ) {
      setFileExpanded(revealPath, true);
      return;
    }
    handledReveal.current = revealPath;
    openingRevealPath.current = null;
    // Let CodeView lay out the expanded item before scrolling to it.
    const isCurrent = startPlacement();
    afterLayout(() => {
      if (isCurrent()) scrollToFile(revealPath);
    });
  }, [
    revealPath,
    revealTarget,
    filesByPath,
    searchedPaths,
    items,
    contents,
    collapsed,
    viewedPaths,
    setFileExpanded,
    scrollToFile,
    startPlacement,
  ]);

  // A line request for a rich file shows its source diff, where lines and comments live.
  // Each request switches once, so the reader can turn the rich diff back on.
  const setRich = markdown.setRich;
  const latestPreviewStates = useRef(markdown.states);
  latestPreviewStates.current = markdown.states;
  // The reader's own switch keeps that file where it was on screen, while its new
  // view loads and renders, rather than leaving the scroll offset to land elsewhere.
  const setRichInPlace = useCallback(
    (path: string, rich: boolean) => {
      const instance = viewer.current?.getInstance();
      const container = instance?.getContainerElement();
      const id = reviewItemId(path);
      const element = instance?.getRenderedItems().find((rendered) => rendered.id === id)?.element;
      setRich(path, rich);
      if (!container || !element) return;
      const offset = fileAnchorOffset(
        element.getBoundingClientRect().top,
        container.getBoundingClientRect().top,
      );
      // Settled once the view asked for has rendered, which comes a little after the click.
      alignRendered(id, undefined, offset, startPlacement(), {
        settled: () =>
          switchSettled(
            rich,
            committedRich.current.has(path),
            latestPreviewStates.current.get(path),
          ),
        budgetMs: FULL_TEXT_BUDGET_MS,
      });
    },
    [setRich, alignRendered, startPlacement],
  );
  // The shared switch flips the reader's latest choice, not what shows now.
  const isIntendedRich = markdown.isIntendedRich;
  const toggleRichInPlace = useCallback(
    (path: string) => setRichInPlace(path, !isIntendedRich(path)),
    [isIntendedRich, setRichInPlace],
  );
  /** Run `next` once the diff view has rendered `path` as `rich`, or the budget runs out. */
  const afterRendered = useCallback(
    (path: string, rich: boolean, next: () => void, isCurrent: () => boolean) => {
      const deadline = performance.now() + REVEAL_RENDER_BUDGET_MS;
      const look = () => {
        if (!isCurrent()) return;
        if (committedRich.current.has(path) === rich || performance.now() > deadline) {
          afterLayout(next);
          return;
        }
        requestAnimationFrame(look);
      };
      look();
    },
    [],
  );
  // A line request withdraws a rich choice even before it shows; the reveal then waits for the
  // source diff to render.
  const intendedRichPaths = markdown.intendedRichPaths;
  const switchedForTarget = useRef<ReviewRevealTarget | null>(null);
  useEffect(() => {
    if (!revealTarget || switchedForTarget.current === revealTarget) return;
    switchedForTarget.current = revealTarget;
    if (intendedRichPaths.has(revealTarget.path)) setRich(revealTarget.path, false);
  }, [revealTarget, intendedRichPaths, setRich]);

  // Find: matches come from the model; revealing one expands its file and paints it. This
  // review owns its highlights, so another review's find never clears them, nor this one its.
  const [findOwner] = useState(() => ({}));
  const findNames = source?.findHighlightNames ?? DIFF_FIND_HIGHLIGHTS;
  const latestFindNames = useRef(findNames);
  latestFindNames.current = findNames;
  // One find request at a time: each reveal or clear starts a new one, and anything still
  // queued for an older one, such as a delayed reveal or paint, does nothing.
  const findRequest = useRef(0);
  // The match the current request shows, with the content it was found in, so a repaint
  // never paints it into a file whose text has since been replaced.
  const currentFind = useRef<{
    match: ReviewSearchMatch;
    request: number;
    content: DiffReviewContent | undefined;
  } | null>(null);
  const latestContents = useRef(contents);
  latestContents.current = contents;
  const renderedElement = useCallback(
    (id: string) =>
      viewer.current
        ?.getInstance()
        ?.getRenderedItems()
        .find((item) => item.id === id)?.element,
    [],
  );
  const isFindRequest = useCallback((request: number) => request === findRequest.current, []);
  const startFindRequest = useCallback(() => {
    findRequest.current += 1;
    currentFind.current = null;
    clearOwnedFindHighlights(findOwner);
    return findRequest.current;
  }, [findOwner]);
  const paintWhenRendered = useCallback(
    (match: ReviewSearchMatch, request: number) => {
      if (!isFindRequest(request)) return;
      currentFind.current = {
        match,
        request,
        content: latestContents.current.get(pathFromReviewItemId(match.id)),
      };
      const attempt = (remaining: number) => {
        if (!isFindRequest(request)) return;
        const rendered = renderedElement(match.id);
        if (rendered && paintCurrentMatch(findOwner, latestFindNames.current, rendered, match)) {
          return;
        }
        if (remaining > 0) requestAnimationFrame(() => attempt(remaining - 1));
      };
      requestAnimationFrame(() => attempt(30));
    },
    [findOwner, isFindRequest, renderedElement],
  );
  // Rows render and recycle as the reader scrolls or the layout changes: after each render the
  // current match is painted again where its row now is, and only this review's range is cleared
  // while the row is gone. It never scrolls or moves to another match.
  const findRepaintFrame = useRef<number | undefined>(undefined);
  const scheduleFindRepaint = useCallback(() => {
    if (findRepaintFrame.current !== undefined) return;
    findRepaintFrame.current = requestAnimationFrame(() => {
      findRepaintFrame.current = undefined;
      const current = currentFind.current;
      if (!current || !isFindRequest(current.request)) return;
      const path = pathFromReviewItemId(current.match.id);
      const rendered =
        latestContents.current.get(path) === current.content
          ? renderedElement(current.match.id)
          : undefined;
      if (
        rendered &&
        paintCurrentMatch(findOwner, latestFindNames.current, rendered, current.match)
      ) {
        return;
      }
      clearOwnedFindHighlights(findOwner);
    });
  }, [findOwner, isFindRequest, renderedElement]);

  // Bring a line into view, expanding its file and any hidden unchanged lines
  // around it first; `scroll` runs once the line can be rendered, unless the
  // placement stopped being current in the meantime.
  const revealLineThen = useCallback(
    (
      id: string,
      lineNumber: number,
      side: "additions" | "deletions" | undefined,
      scroll: () => void,
      isCurrent: () => boolean,
    ) => {
      // Loading a patch's full text is a network request, so it gets longer than a render.
      const loadDeadline = performance.now() + FULL_TEXT_BUDGET_MS;
      // One attempt per version of the diff: a refresh mid-reveal brings a new one to load.
      const loadAttempts = new WeakMap<FileDiffMetadata, { failed: boolean }>();
      const reveal = (deadline: number) => {
        if (!isCurrent()) return;
        if (!side) return scroll();
        const rendered = viewer.current
          ?.getInstance()
          ?.getRenderedItems()
          .find((item) => item.id === id);
        if (rendered?.type !== "diff") {
          // Hidden context can only expand once the file's diff is rendered,
          // which on first open can take several layout passes.
          if (performance.now() > deadline) return scroll();
          viewer.current?.scrollTo({ type: "item", id, align: "start", behavior: "instant" });
          return afterLayout(() => reveal(deadline));
        }
        // A patch reveals a line it left out once its full text loads; lines it has,
        // including every changed line, scroll right away. One reveal tries one load;
        // a failed load falls back, and a later reveal or expansion tries again.
        const fileDiff = rendered.item.fileDiff;
        const loadText = latestLoadFullText.current;
        if (fileDiff.isPartial && loadText && !hunksHaveLine(fileDiff, lineNumber, side)) {
          let attempt = loadAttempts.get(fileDiff);
          if (!attempt) {
            const started = { failed: false };
            attempt = started;
            loadAttempts.set(fileDiff, started);
            loadText(fileDiff).catch(() => {
              started.failed = true;
            });
          }
          if (attempt.failed || performance.now() > loadDeadline) return scroll();
          return afterLayout(() => reveal(deadline));
        }
        // Hidden context expands by current-version lines, so map an old-version line first.
        const currentLine =
          side === "additions" ? lineNumber : currentLineForOldLine(fileDiff, lineNumber);
        if (currentLine === undefined || rendered.instance.isLineRenderable(currentLine)) {
          return scroll();
        }
        rendered.instance.revealLine(currentLine);
        afterLayout(() => {
          if (isCurrent()) scroll();
        });
      };
      if (viewer.current?.getItem(id)?.collapsed) {
        setFileExpanded(pathFromReviewItemId(id), true);
        afterLayout(() => reveal(performance.now() + REVEAL_RENDER_BUDGET_MS));
      } else {
        reveal(performance.now() + REVEAL_RENDER_BUDGET_MS);
      }
    },
    [setFileExpanded],
  );

  // A rich diff's match is in one of its sections: expand the file, then bring that section into view.
  const revealPreviewMatch = useCallback(
    (match: ReviewSearchMatch, request: number) => {
      const isPlaced = startPlacement();
      const isCurrent = () => isPlaced() && isFindRequest(request);
      const show = () => {
        if (!isCurrent()) return;
        viewer.current?.scrollTo({
          type: "item",
          id: match.id,
          align: "start",
          behavior: "instant",
        });
        // A tall block can hold its match far below its top: centre the matched text itself,
        // correcting as blocks above it lay out.
        const centre = (element: HTMLElement, corrections: number) => {
          if (!isCurrent()) return;
          const container = viewer.current?.getInstance()?.getContainerElement();
          const bounds = searchMatchRange(element, match)?.getBoundingClientRect();
          if (!container || !bounds || bounds.height === 0) return;
          const view = container.getBoundingClientRect();
          const drift = bounds.top + bounds.height / 2 - (view.top + view.height / 2);
          if (Math.abs(drift) < 1 || corrections === 0) return;
          container.scrollBy({ top: drift, behavior: "instant" });
          requestAnimationFrame(() => centre(element, corrections - 1));
        };
        const attempt = (remaining: number) => {
          if (!isCurrent()) return;
          const element = viewer.current
            ?.getInstance()
            ?.getRenderedItems()
            .find(({ id }) => id === match.id)?.element;
          const section = element?.querySelector(
            `[${MARKDOWN_DIFF_SECTION_ATTRIBUTE}="${match.lineNumber}"]`,
          );
          if (element && section) {
            section.scrollIntoView({ block: "start", behavior: "instant" });
            requestAnimationFrame(() => centre(element, MAX_ALIGN_CORRECTIONS));
            paintWhenRendered(match, request);
            return;
          }
          // The section may still be waiting for its batch to render.
          if (remaining > 0) requestAnimationFrame(() => attempt(remaining - 1));
        };
        afterLayout(() => attempt(120));
      };
      if (viewer.current?.getItem(match.id)?.collapsed) {
        setFileExpanded(pathFromReviewItemId(match.id), true);
        afterLayout(show);
      } else {
        show();
      }
    },
    [isFindRequest, paintWhenRendered, setFileExpanded, startPlacement],
  );

  const revealMatch = useCallback(
    (match: ReviewSearchMatch) => {
      const request = startFindRequest();
      // Every delayed step of this reveal stops once a newer find request starts.
      const fenced = (isPlaced: () => boolean) => () => isPlaced() && isFindRequest(request);
      if (match.target === "markdownPreview") {
        revealPreviewMatch(match, request);
        return;
      }
      const revealSourceLine = () =>
        revealLineThen(
          match.id,
          match.lineNumber,
          match.side,
          () => {
            viewer.current?.scrollTo({
              type: "line",
              id: match.id,
              lineNumber: match.lineNumber,
              side: match.side,
              align: "center",
              behavior: "instant",
            });
            paintWhenRendered(match, request);
          },
          fenced(startPlacement()),
        );
      const path = pathFromReviewItemId(match.id);
      if (!match.target && shownRich.has(path)) {
        // A preview that can't show this file leaves the match in its source diff alone.
        const status = markdown.states.get(path)?.status;
        if (status === "failed" || status === "unavailable") {
          markdown.setRich(path, false);
          afterRendered(path, false, revealSourceLine, fenced(startPlacement()));
          return;
        }
        // Still loading: show the file, whose sections become searchable once ready.
        if (viewer.current?.getItem(match.id)?.collapsed) {
          setFileExpanded(pathFromReviewItemId(match.id), true);
        }
        afterLayout(() => {
          if (!isFindRequest(request)) return;
          viewer.current?.scrollTo({
            type: "item",
            id: match.id,
            align: "start",
            behavior: "instant",
          });
        });
        return;
      }
      if (match.target) {
        viewer.current?.scrollTo({
          type: "item",
          id: match.id,
          align: "start",
          behavior: "instant",
        });
        paintWhenRendered(match, request);
        return;
      }
      revealSourceLine();
    },
    [
      afterRendered,
      isFindRequest,
      markdown,
      paintWhenRendered,
      revealLineThen,
      revealPreviewMatch,
      setFileExpanded,
      shownRich,
      startFindRequest,
      startPlacement,
    ],
  );

  const clearMatch = useCallback(() => {
    startFindRequest();
  }, [startFindRequest]);

  // A requested line is revealed once its file's diff is ready; a persistent
  // request is revealed again whenever that diff refreshes, until the reader takes over.
  const handledTarget = useRef<{ target: ReviewRevealTarget; content: unknown } | null>(null);
  useEffect(() => {
    const target = revealTarget;
    if (!target || abandonedTarget.current === target || target.persistent?.() === false) return;
    const content = contents.get(target.path);
    if (content?.status !== "ready") return;
    // A rich file has no lines to reveal until it shows its source diff.
    if (shownRich.has(target.path)) return;
    const handled = handledTarget.current;
    if (handled?.target === target && (handled.content === content || !target.persistent)) {
      return;
    }
    if (!searchedPaths.has(target.path)) {
      setTreeSearch(null);
      return;
    }
    if (!listedFiles.some((file) => file.path === target.path)) {
      setPinnedPath(target.path);
      return;
    }
    handledTarget.current = { target, content };
    pendingAnchor.current = undefined;
    const id = reviewItemId(target.path);
    const side = target.side ?? "additions";
    const isCurrent = startPlacement();
    const renderedRow = () => {
      const root = viewer.current
        ?.getInstance()
        ?.getRenderedItems()
        .find((item) => item.id === id)?.element.shadowRoot;
      return root ? sideRow(root, target.line, side) : undefined;
    };
    const revealTargetColumn = () => {
      const column = target.column;
      if (!column || wrapLines) return;
      afterLayout(() => {
        const row = isCurrent() ? renderedRow() : undefined;
        if (row) revealColumn(row, column - 1);
      });
    };
    revealLineThen(
      id,
      target.line,
      side,
      () => {
        const container = viewer.current?.getInstance()?.getContainerElement();
        if (target.align === "nearest" && container) {
          const bounds = renderedRow()?.getBoundingClientRect();
          const viewport = container.getBoundingClientRect();
          if (bounds && bounds.top >= viewport.top && bounds.bottom <= viewport.bottom) return;
        }
        const endLine = target.endLine ?? target.line;
        const height = container?.getBoundingClientRect().height ?? 0;
        if (endLine > target.line && rangeFits(target.line, endLine, lineHeight, height)) {
          viewer.current?.scrollTo({
            type: "range",
            id,
            range: { start: target.line, end: endLine, side },
            align: "center",
            behavior: "instant",
          });
        } else {
          viewer.current?.scrollTo({
            type: "line",
            id,
            lineNumber: target.line,
            side,
            align: endLine > target.line ? "start" : "center",
            behavior: "instant",
          });
        }
        revealTargetColumn();
      },
      isCurrent,
    );
  }, [
    revealTarget,
    contents,
    shownRich,
    searchedPaths,
    listedFiles,
    revealLineThen,
    lineHeight,
    startPlacement,
    wrapLines,
  ]);

  // Restore the reader's place once the file it was in has its diff.
  useEffect(() => {
    const anchor = pendingAnchor.current;
    if (!anchor) return;
    if (!filesByPath.has(anchor.path) || !listedFiles.some((file) => file.path === anchor.path)) {
      // The file is gone or filtered out: fall back to revealing the opened file.
      pendingAnchor.current = undefined;
      return;
    }
    // Files above the anchor change height when their diffs arrive, so wait for them too.
    const anchorIndex = listedFiles.findIndex((file) => file.path === anchor.path);
    const settled = listedFiles
      .slice(0, anchorIndex + 1)
      .every((file) => file.isBinary || contents.get(file.path)?.status !== "loading");
    // Its own file must have settled too; one listed before its content counts as not yet.
    const anchorStatus = contents.get(anchor.path)?.status;
    if (!settled || anchorStatus === undefined || anchorStatus === "loading") return;
    pendingAnchor.current = undefined;
    if (revealPath) handledReveal.current = revealPath;
    const id = reviewItemId(anchor.path);
    const isCurrent = startPlacement();
    // A rich file, or one that settled without a text diff, has no lines, so its place is
    // restored to the file.
    if (anchor.line === undefined || anchorStatus !== "ready" || shownRich.has(anchor.path)) {
      afterLayout(() => {
        if (!isCurrent()) return;
        viewer.current?.scrollTo({ type: "item", id, align: "start", behavior: "instant" });
        alignRendered(id, undefined, anchor.offset, isCurrent);
      });
      return;
    }
    const line = anchor.line;
    revealLineThen(
      id,
      line,
      "additions",
      () => {
        viewer.current?.scrollTo({
          type: "line",
          id,
          lineNumber: line,
          side: "additions",
          align: "start",
          behavior: "instant",
        });
        alignRendered(id, line, anchor.offset, isCurrent);
      },
      isCurrent,
    );
  }, [
    contents,
    shownRich,
    filesByPath,
    listedFiles,
    revealPath,
    revealLineThen,
    startPlacement,
    alignRendered,
  ]);

  const textLayer = useReviewTextLayer({
    viewer,
    shell,
    contents,
    highlights,
    onTextEvent: useCallback(
      (event: ReviewTextEvent) => {
        // A file the reader acts on in place becomes current without scrolling back to its top.
        if ("path" in event && event.path && event.path !== revealPath) {
          focusedHere.current = event.path;
        }
        onTextEvent?.(event);
      },
      [onTextEvent, revealPath],
    ),
  });

  // The file at the viewport's top, which ⌘K's file commands act on.
  const [anchorPath, setAnchorPath] = useState<string | null>(null);
  const latestAnchorChange = useRef(onScrollAnchorChange);
  latestAnchorChange.current = onScrollAnchorChange;
  const anchorFrame = useRef<number | undefined>(undefined);
  useEffect(
    () => () => {
      if (anchorFrame.current !== undefined) cancelAnimationFrame(anchorFrame.current);
    },
    [],
  );
  const { onScroll: onTextLayerScroll } = textLayer;
  const handleScroll = useCallback(
    (
      _scrollTop: number,
      view: Parameters<CodeViewScrollListener<A>>[1],
      // Optional: the engine patch adds it; without it, every scroll counts as someone else's.
      details?: Parameters<CodeViewScrollListener<A>>[2],
    ) => {
      onTextLayerScroll();
      // A scroll the width restore's own correction or the view's own made is theirs; any other,
      // a script's, find in page's or assistive technology's, takes the view from the restore.
      if (isForeignScroll(ownScrollWrite, view, details)) {
        yieldWidthRestore.current?.();
        yieldWidthRestore.current = null;
      }
      // Reading rows on every scroll event is wasted work; once per frame is enough.
      if (anchorFrame.current !== undefined) return;
      anchorFrame.current = requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          anchorFrame.current = undefined;
          const instance = viewer.current?.getInstance();
          const container = instance?.getContainerElement();
          if (container) captureReadingAnchor(container);
          if (!instance || !container) return;
          if (pendingAnchor.current && anchorSource.current === "saved") return;
          const anchor = readScrollAnchor(
            container.getBoundingClientRect().top,
            instance.getRenderedItems(),
          );
          if (!anchor) return;
          // The reader's own scroll among files still loading: keep their place for when the
          // diffs above it arrive and grow. The view's own corrections don't move it, and a file
          // they picked holds until their next input.
          const pickedFile = !!pendingAnchor.current && anchorSource.current === "file";
          if (!details?.engineOwned && !pickedFile) {
            pendingAnchor.current = loadingThrough.current(anchor.path) ? anchor : undefined;
            anchorSource.current = "reader";
          }
          latestAnchorChange.current?.(anchor);
          setAnchorPath(anchor.path);
        }),
      );
    },
    [captureReadingAnchor, onTextLayerScroll],
  );

  const sourceSelection = source?.selectedLines;
  const selectedLines = useMemo(() => {
    if (sourceSelection !== undefined) {
      return sourceSelection
        ? { id: reviewItemId(sourceSelection.path), range: sourceSelection.range }
        : null;
    }
    return markedLines
      ? {
          id: reviewItemId(markedLines.path),
          range: { start: markedLines.start, end: markedLines.end, side: "additions" as const },
        }
      : null;
  }, [sourceSelection, markedLines]);

  const startingIndex = useCallback(
    (matches: readonly ReviewSearchMatch[]) => {
      const instance = viewer.current?.getInstance();
      const container = instance?.getContainerElement();
      if (!instance || !container) return 0;
      const viewportTop = container.getBoundingClientRect().top;
      const rendered = instance.getRenderedItems();
      const renderedById = new Map(rendered.map((item) => [item.id, item.element]));
      const renderedIndex = firstRenderedMatchAtOrAfter(renderedById, matches, viewportTop);
      if (renderedIndex !== undefined) return renderedIndex;
      const order = new Map(items.map((item, index) => [item.id, index]));
      const firstVisible = rendered.find(
        ({ element }) => element.getBoundingClientRect().bottom >= viewportTop,
      );
      const firstVisibleIndex = firstVisible ? order.get(firstVisible.id) : undefined;
      if (firstVisibleIndex === undefined) return 0;
      const next = matches.findIndex((match) => (order.get(match.id) ?? -1) > firstVisibleIndex);
      return next < 0 ? 0 : next;
    },
    [items],
  );

  const find = useDiffReviewFind(
    findOpen,
    searchLines,
    { reveal: revealMatch, clear: clearMatch, startingIndex },
    hiddenSearchLines,
  );

  // Opening find seeds it with the text selected in the diff, like other finds. A layout
  // effect reads the selection before the find bar's focus moves it into the query field,
  // then asks the bar to focus again so the seeded query is what's selected.
  const { setQuery } = find;
  const [seededFocus, setSeededFocus] = useState(0);
  useLayoutEffect(() => {
    if (!findOpen) return;
    const selection = document.getSelection();
    const text = selection?.toString();
    // Diff rows render in each file's shadow root, so a plain contains() never sees them.
    if (text?.trim() && shell && containsAcrossShadowRoots(shell, selection?.anchorNode ?? null)) {
      setQuery(findQueryFromSelection(text));
      setSeededFocus((count) => count + 1);
    }
  }, [findOpen, findFocusRequest, setQuery, shell]);

  // A match found in another review's scope, or painted under other names, never repaints here.
  useEffect(
    () => () => {
      startFindRequest();
      if (findRepaintFrame.current !== undefined) cancelAnimationFrame(findRepaintFrame.current);
      findRepaintFrame.current = undefined;
    },
    [reviewScope, findNames.all, findNames.current, startFindRequest],
  );

  const languages = useMemo(() => {
    const set = new Set<SupportedLanguages>(["text"]);
    for (const file of files) set.add(syntaxLanguage(file.path));
    return [...set];
  }, [files]);
  // The shared pool once it's started; before that, or after it fails, its own as before.
  const workers = useDiffWorkers();
  const highlighterOptions = useMemo(
    () => ({ langs: languages, theme: DIFF_SYNTAX_THEME }),
    [languages],
  );
  const poolOptions = useMemo(
    () => ({
      poolSize: Math.max(2, Math.min(4, navigator.hardwareConcurrency || 2)),
      workerFactory: () =>
        new Worker(new URL("@pierre/diffs/worker/worker.js", import.meta.url), { type: "module" }),
    }),
    [],
  );
  const onTextLayerPostRender = textLayer.onPostRender;
  const onLineSelection = source?.onLineSelection;
  const expandContext = source?.expandContext;
  const hasGutterUtility = !!source?.renderGutterUtility;
  const selectionCallbacks = useMemo(() => {
    if (!onLineSelection) return {};
    const report =
      (phase: DiffReviewSelectionPhase) =>
      (range: SelectedLineRange | null, context: { item: { id: string } }) =>
        onLineSelection(pathFromReviewItemId(context.item.id), range, phase);
    return {
      onLineSelectionStart: report("preview"),
      onLineSelectionChange: report("preview"),
      onLineSelectionEnd: report("commit"),
    };
  }, [onLineSelection]);
  const options = useMemo(
    () => ({
      theme: DIFF_SYNTAX_THEME,
      themeType,
      diffStyle: shownLayout,
      diffIndicators: "classic" as const,
      hunkSeparators: "line-info-basic" as const,
      lineDiffType: "word-alt" as const,
      overflow: wrapLines ? ("wrap" as const) : ("scroll" as const),
      stickyHeaders: true,
      enableLineSelection: true,
      enableGutterUtility: hasGutterUtility,
      ...(expandContext &&
        loadFullText && {
          loadDiffFiles: loadFullText,
          expansionLineCount: expandContext.lineCount,
        }),
      ...selectionCallbacks,
      onRenderError,
      unsafeCSS: SHADOW_CSS + textLayer.highlightCss,
      onPostRender: (...render: Parameters<typeof onTextLayerPostRender>) => {
        onTextLayerPostRender(...render);
        markRenderedContextLoads();
        notifyRendered();
        scheduleFindRepaint();
      },
      // A file header's height as it draws: a short estimate shifts every file CodeView measures
      // later, so a long run of collapsed files moves under the reader as it renders.
      itemMetrics: {
        lineHeight,
        diffHeaderHeight: DIFF_FILE_HEADER_HEIGHT,
        spacing: 0,
        paddingBottom: 0,
      },
      layout: { gap: 8, paddingTop: 0, paddingBottom: 16 },
    }),
    [
      themeType,
      shownLayout,
      wrapLines,
      lineHeight,
      textLayer.highlightCss,
      onTextLayerPostRender,
      markRenderedContextLoads,
      notifyRendered,
      scheduleFindRepaint,
      hasGutterUtility,
      expandContext,
      loadFullText,
      selectionCallbacks,
      onRenderError,
    ],
  );

  const renderHeaderPrefix = useCallback(
    (item: CodeViewItem<A>) => {
      const path = pathFromReviewItemId(item.id);
      const file = filesByPath.get(path);
      // Every file's header names its path, from the item each render, so a pooled item
      // reused for another file never keeps the last one's.
      if (!file || !isExpandable(file, contents.get(path))) {
        return (
          <span data-github-file={path} className="inline-flex">
            <span className="inline-block size-5" aria-hidden="true" />
          </span>
        );
      }
      const expanded = !item.collapsed;
      return (
        <span data-github-file={path} className="inline-flex">
          <Button
            type="button"
            variant="ghost"
            size="icon-xs"
            aria-label={`${expanded ? "Collapse" : "Expand"} ${path}`}
            aria-expanded={expanded}
            onClick={(event) => {
              event.stopPropagation();
              setFileExpanded(path, !expanded);
            }}
          >
            {expanded ? <ChevronDownIcon /> : <ChevronRightIcon />}
          </Button>
        </span>
      );
    },
    [contents, filesByPath, setFileExpanded],
  );

  // Each file's title copy button, so its ⋯ menu repeats Copy path only while a narrow header hides it.
  const titleCopyButtons = useRef(new Map<string, HTMLElement>());
  const titleShowsCopy = useCallback((path: string) => {
    const button = titleCopyButtons.current.get(path);
    return !!button?.isConnected && isShown(button);
  }, []);
  const renderHeaderExtras = source?.renderHeaderExtras;
  // A file's menu: Open in new tab first, where the surface has tabs, then the source's actions.
  const latestOpenInNewTab = useRef(onOpenFileInNewTab);
  latestOpenInNewTab.current = onOpenFileInNewTab;
  const opensInNewTab = onOpenFileInNewTab !== undefined;
  const menuActionsFor = useCallback(
    (file: DiffReviewFile): readonly DiffReviewFileAction[] => {
      const actions = fileActions?.(file) ?? [];
      if (!opensInNewTab) return actions;
      const open = newTabFileAction(file.path, (path, tabOptions) =>
        latestOpenInNewTab.current?.(path, tabOptions),
      );
      return [open, ...actions];
    },
    [fileActions, opensInNewTab],
  );
  const showChangeBadges = presentation?.fileBadges !== "icons";
  const renderHeaderMetadata = useCallback(
    (item: CodeViewItem<A>) => {
      const path = pathFromReviewItemId(item.id);
      const file = filesByPath.get(path);
      if (!file) return null;
      const content = contents.get(path);
      const contextLoad = contextLoadsByPath.get(path);
      const richAction = canPreviewMarkdown(file, content)
        ? markdownDiffViewAction(path, shownRich.has(path), toggleRichInPlace)
        : undefined;
      const actions = menuActionsFor(file);
      return (
        <span className="flex items-center gap-1.5 text-xs">
          {/* A narrow header keeps the name: the file's icon already shows the change. */}
          {showChangeBadges && file.change === "added" && (
            <Badge variant="outline" className="@max-[22rem]:hidden">
              Added
            </Badge>
          )}
          {showChangeBadges && file.change === "deleted" && (
            <Badge variant="outline" className="@max-[22rem]:hidden">
              Deleted
            </Badge>
          )}
          {richAction && <RichDiffToggle path={path} action={richAction} />}
          {file.ownedByViewer && ownedFiles && (
            <OwnedFilesToggle command={ownedFilesCommand(ownedFiles)} />
          )}
          {file.isGenerated && <Badge variant="secondary">Generated</Badge>}
          {file.isBinary && <Badge variant="secondary">Binary</Badge>}
          {file.notes?.map((note) => (
            <Badge key={note} variant="secondary">
              {note}
            </Badge>
          ))}
          <FileStatusNote file={file} content={content} />
          <RetryFileButton content={content} onRetry={() => onRetry(path)} />
          <ContextLoadStatus
            path={path}
            state={contextLoad?.state}
            onRetry={() => contextLoad && retryContextLoad(contextLoad.fileDiff)}
          />
          {renderHeaderExtras?.(file)}
          {viewedMarksOffered && (
            <ViewedToggle
              path={path}
              viewed={viewedPaths.has(path)}
              unavailableReason={viewedBlockedReason(file)}
              onToggle={() => toggleFileViewed(file)}
            />
          )}
          <FileActionsMenu
            path={path}
            actions={richAction ? [richAction, ...actions] : actions}
            titleShowsCopy={titleShowsCopy}
          />
        </span>
      );
    },
    [
      contents,
      contextLoadsByPath,
      retryContextLoad,
      filesByPath,
      onRetry,
      menuActionsFor,
      ownedFiles,
      renderHeaderExtras,
      shownRich,
      toggleRichInPlace,
      viewedPaths,
      viewedBlockedReason,
      toggleFileViewed,
      viewedMarksOffered,
      titleShowsCopy,
      showChangeBadges,
    ],
  );

  const renderHeaderFilenameSuffix = useCallback(
    (item: CodeViewItem<A>) => {
      const path = pathFromReviewItemId(item.id);
      const file = filesByPath.get(path);
      if (!file) return null;
      const copyPath = fileActions?.(file).find(
        (action) => action.id === "copy-path" && !action.disabledReason,
      );
      return (
        <ReviewFileTitle
          path={path}
          copyPath={copyPath}
          copyButtonRef={(button) => {
            const buttons = titleCopyButtons.current;
            if (button) buttons.set(path, button);
            else if (buttons.get(path)?.isConnected === false) buttons.delete(path);
          }}
        />
      );
    },
    [filesByPath, fileActions],
  );

  const sourceRenderAnnotation = source?.renderAnnotation;
  const renderAnyAnnotation = useCallback(
    (annotation: LineAnnotation<A> | DiffLineAnnotation<A>, item: CodeViewItem<A>) => {
      const path = pathFromReviewItemId(item.id);
      if (isMarkdownPreviewAnnotation(annotation.metadata)) {
        const content = contents.get(path);
        if (content?.status !== "ready") return null;
        return (
          <MarkdownPreviewSlot
            path={path}
            fileDiff={content.fileDiff}
            markdown={markdown}
            setRich={setRichInPlace}
            onResize={onPreviewResize}
            hasSourceComments={
              (sourceAnnotations?.get(path)?.length ?? 0) > 0 ||
              highlights.some((highlight) => highlight.path === path)
            }
          />
        );
      }
      return sourceRenderAnnotation?.(annotation as DiffLineAnnotation<A>, path) ?? null;
    },
    [
      contents,
      markdown,
      setRichInPlace,
      onPreviewResize,
      sourceAnnotations,
      highlights,
      sourceRenderAnnotation,
    ],
  );
  // Without annotations to show, CodeView gets no renderer at all.
  const renderAnnotation =
    sourceRenderAnnotation || shownRich.size > 0 ? renderAnyAnnotation : undefined;

  const paletteSource = useId();
  const currentPath = anchorPath ?? revealPath ?? null;
  const markdownPaletteCommands = useMemo((): PaletteCommand[] => {
    if (!commandsActive) return NO_COMMANDS;
    const file = currentPath ? filesByPath.get(currentPath) : undefined;
    if (!file || !canPreviewMarkdown(file, contents.get(file.path))) return NO_COMMANDS;
    const action = markdownDiffViewAction(file.path, shownRich.has(file.path), toggleRichInPlace);
    const name = file.path.slice(file.path.lastIndexOf("/") + 1);
    return [
      {
        id: action.id,
        label: `${action.label} for ${name}`,
        icon: action.icon,
        keywords: ["markdown", "rendered", "preview", "diff", file.path],
        run: action.run,
      },
    ];
  }, [commandsActive, currentPath, filesByPath, contents, shownRich, toggleRichInPlace]);
  usePaletteCommands(
    `diff-review-markdown${paletteSource}`,
    commandHeading,
    markdownPaletteCommands,
  );
  // The current file's header actions, so ⌘K acts on the file being read. A
  // file the filters hide gives way to the listed file now in its place.
  const currentFile = useMemo(
    () => listedFileAt(currentPath, files, listedFiles),
    [currentPath, files, listedFiles],
  );
  const viewedPaletteCommands = useMemo((): PaletteCommand[] => {
    const file = currentFile;
    if (!viewedMarksOffered || !commandsActive || !file || viewedBlockedReason(file)) {
      return NO_COMMANDS;
    }
    const viewed = viewedPaths.has(file.path);
    const name = file.path.slice(file.path.lastIndexOf("/") + 1);
    return [
      {
        id: "toggle-viewed",
        label: viewed ? `Mark ${name} as not viewed` : `Mark ${name} as viewed`,
        icon: CheckIcon,
        keywords: ["viewed", "reviewed", "done", "seen", file.path],
        run: () => toggleFileViewed(file),
      },
    ];
  }, [
    viewedMarksOffered,
    commandsActive,
    currentFile,
    viewedPaths,
    viewedBlockedReason,
    toggleFileViewed,
  ]);
  usePaletteCommands(
    `diff-review-viewed${paletteSource}:${currentFile?.path ?? ""}`,
    commandHeading,
    viewedPaletteCommands,
  );
  // Marks or clears every listed file at once; a filtered list says so, since only it changes.
  const markAllViewedPaletteCommands = useMemo((): PaletteCommand[] => {
    if (!viewedMarksOffered || !commandsActive || listedFiles.length === 0) return NO_COMMANDS;
    const batch = viewedBatch(listedFiles, viewedPaths, contents);
    const scope = listedFiles.length < files.length ? "all listed files" : "all files";
    const commands: PaletteCommand[] = [];
    if (batch.toMark.length > 0 || batch.loading) {
      commands.push({
        id: "mark-all-viewed",
        label: `Mark ${scope} as viewed`,
        icon: CheckCheckIcon,
        keywords: ["viewed", "reviewed", "done", "seen", "every file"],
        availability: batch.loading
          ? unavailableBecause(MARK_ALL_VIEWED_LOADING_REASON)
          : AVAILABLE,
        run: () => {
          for (const { path, fingerprint } of batch.toMark) {
            markViewed(reviewScope, path, fingerprint);
          }
          forgetCollapsed(batch.toMark.map(({ path }) => path));
        },
      });
    }
    if (batch.toClear.length > 0) {
      commands.push({
        id: "mark-all-not-viewed",
        label: `Mark ${scope} as not viewed`,
        icon: XIcon,
        keywords: ["viewed", "unviewed", "reviewed", "reset", "every file"],
        run: () => {
          for (const path of batch.toClear) clearViewed(reviewScope, path);
          forgetCollapsed(batch.toClear);
        },
      });
    }
    return commands;
  }, [
    viewedMarksOffered,
    commandsActive,
    listedFiles,
    files.length,
    viewedPaths,
    contents,
    reviewScope,
    forgetCollapsed,
  ]);
  usePaletteCommands(
    `diff-review-viewed-all${paletteSource}`,
    commandHeading,
    markAllViewedPaletteCommands,
  );
  // A folder focused in this review's tree has ⌘K, with its menu's actions, instead of the file.
  // Matched by owner, not DOM: a narrow review's tree is a popover portalled out of it.
  const folderOwner = useId();
  const focusedFolder = useFocusedReviewFolder();
  const ownFocusedFolder =
    focusedFolder?.folder.owner === folderOwner ? focusedFolder.folder : null;
  const folderPaletteCommands = useMemo((): PaletteCommand[] => {
    if (!commandsActive || !ownFocusedFolder) return NO_COMMANDS;
    return ownFocusedFolder.actions.map((action) => ({
      id: `folder.${action.id}`,
      label: action.paletteLabel ?? action.label,
      icon: action.icon,
      keywords: ["folder", ownFocusedFolder.path, ...(action.keywords ?? [])],
      availability: action.disabledReason ? unavailableBecause(action.disabledReason) : AVAILABLE,
      run: action.run,
    }));
  }, [commandsActive, ownFocusedFolder]);
  usePaletteCommands(
    `diff-review-folder${paletteSource}:${ownFocusedFolder?.path ?? ""}`,
    "Folder",
    folderPaletteCommands,
  );
  const fileActionPaletteCommands = useMemo((): PaletteCommand[] => {
    if (!commandsActive || !fileActionsInPalette || !fileActions || !currentFile) {
      return NO_COMMANDS;
    }
    if (ownFocusedFolder) return NO_COMMANDS;
    return fileActions(currentFile).map((action) => ({
      id: `file.${action.id}`,
      label: action.paletteLabel ?? action.label,
      icon: action.icon,
      keywords: ["file", currentFile.path, ...(action.keywords ?? [])],
      availability: action.disabledReason ? unavailableBecause(action.disabledReason) : AVAILABLE,
      run: action.run,
    }));
  }, [commandsActive, fileActionsInPalette, fileActions, currentFile, ownFocusedFolder]);
  usePaletteCommands(
    `diff-review-file${paletteSource}:${currentFile?.path ?? ""}`,
    "File",
    fileActionPaletteCommands,
  );
  // The file on screen in its own tab, from the same definition as the panel's file list.
  const newTabPaletteCommands = useMemo((): PaletteCommand[] => {
    if (!commandsActive) return NO_COMMANDS;
    return changedFileCommands({
      shownPath: currentFile?.path ?? null,
      newTab: opensInNewTab,
    }).map((command) => ({
      id: `${command.id}:${command.path}`,
      label: command.paletteLabel,
      icon: SquarePlusIcon,
      keywords: [...command.keywords, command.path],
      availability: command.availability,
      run: () => latestOpenInNewTab.current?.(command.path, { background: true }),
    }));
  }, [commandsActive, currentFile, opensInNewTab]);
  usePaletteCommands(
    `diff-review-file-new-tab${paletteSource}:${currentFile?.path ?? ""}`,
    commandHeading,
    newTabPaletteCommands,
  );
  const sourceRenderGutterUtility = source?.renderGutterUtility;
  const renderGutterUtility = useMemo(
    () =>
      sourceRenderGutterUtility &&
      ((hoveredLine: () => unknown, item: CodeViewItem<A>) =>
        sourceRenderGutterUtility(
          hoveredLine as DiffReviewHoveredLine,
          pathFromReviewItemId(item.id),
        )),
    [sourceRenderGutterUtility],
  );
  const footer = source?.footer;
  // Room past the last file, as the code editors keep, so a line near the end
  // of the review can still be centered when a citation reveals it.
  const renderFooter = useCallback(
    () => (
      <>
        {footer}
        <div aria-hidden data-testid="diff-review-end-space" className="h-[50vh]" />
      </>
    ),
    [footer],
  );

  const fontStyle = {
    "--diffs-font-size": `${fontSize}px`,
    "--diffs-line-height": `${lineHeight}px`,
    "--diffs-font-family": codeFontFamilyForEditor(fontFamily ?? ""),
    "--diffs-font-weight": fontWeight,
    "--diffs-header-font-family": "var(--ui-font-family, var(--font-sans))",
    // Comment tints are the shared code-surface highlight tokens.
    "--diff-review-comment":
      "var(--comment-highlight, color-mix(in oklch, var(--warning) 14%, transparent))",
    "--diff-review-comment-active":
      "var(--comment-highlight-active, color-mix(in oklch, var(--warning) 28%, transparent))",
  } as CSSProperties;

  // Show clears what hides find's other matches, the path search and the file filters, and
  // leaves the layout and viewed marks alone; find then runs over the files it brings back.
  const showHiddenMatches = useCallback(() => {
    setTreeSearch(null);
    dispatchFilter({ type: "clearFilters" });
    // Its note goes with the matches it showed, so typing carries on in the find field.
    setSeededFocus((count) => count + 1);
  }, [dispatchFilter]);

  const counts = useMemo(() => languageCounts(files), [files]);
  const testCount = files.filter((file) => file.isTest).length;
  const generatedCount = files.filter((file) => file.isGenerated).length;

  const treeShown = wide ? treeOpen : narrowTreeOpen;
  const filterCommands = presentation?.filterCommands === true;
  const commands = useMemo(
    () =>
      diffReviewCommands({
        treeShown,
        toggleTree: () =>
          wide ? setTreeOpen((open) => !open) : setNarrowTreeOpen((open) => !open),
        searchFiles: () => {
          if (wide) setTreeOpen(true);
          else setNarrowTreeOpen(true);
          setTreeSearch((current) => current ?? "");
          setTreeSearchFocusRequest((request) => request + 1);
        },
        filtersActive: hasActiveFilters(filters),
        openFilters: () => setFiltersOpen(true),
        hiddenMatches: {
          findOpen,
          count: find.hidden.count,
          show: showHiddenMatches,
        },
        listedFileCount: listedFiles.length,
        openLanguageBreakdown: () => setLanguagesOpen(true),
        testFileView: filters.testFileView,
        setTestFileView: (value) => dispatchFilter({ type: "setTestFileView", value }),
        ownedFiles,
        layout,
        // A narrow review shows unified and hides the choice, keeping the saved one.
        setLayout: canSplit ? onLayoutChange : undefined,
        ignoreWhitespace,
        setIgnoreWhitespace: onIgnoreWhitespaceChange,
        wrapLines,
        setWrapLines: onWrapLinesChange,
        allCollapsed,
        canCollapse: expandablePaths.length > 0,
        toggleAllFiles,
        ...(filterCommands && {
          generatedFiles: {
            shown: filters.showGeneratedFiles,
            count: generatedCount,
            setShown: (value: boolean) => dispatchFilter({ type: "setShowGeneratedFiles", value }),
          },
          languages: {
            counts,
            selected: filters.selectedLanguages,
            showOnly: (language: string | null) =>
              dispatchFilter(
                language === null
                  ? { type: "showAllLanguages" }
                  : { type: "showOnlyLanguage", language },
              ),
          },
        }),
      }),
    [
      findOpen,
      find.hidden.count,
      showHiddenMatches,
      filterCommands,
      listedFiles.length,
      generatedCount,
      ownedFiles,
      counts,
      treeShown,
      wide,
      filters,
      dispatchFilter,
      layout,
      onLayoutChange,
      canSplit,
      ignoreWhitespace,
      onIgnoreWhitespaceChange,
      wrapLines,
      onWrapLinesChange,
      allCollapsed,
      expandablePaths,
      toggleAllFiles,
    ],
  );
  const showHiddenCommand = commands.find((command) => command.id === "show-hidden-matches");
  const commandScope = useId();
  const paletteCommands = useMemo(
    () =>
      !commandsActive || files.length === 0
        ? []
        : diffReviewPaletteCommands(commandScope, commands),
    [commandsActive, commandScope, commands, files.length],
  );
  usePaletteCommands(`diff-review-${commandScope}`, commandHeading, paletteCommands);

  // The tree opens a file row in a new tab, and lists the same menu as its header.
  const openTreeFileInNewTab = useCallback(
    (path: string, tabOptions: { background: boolean }) =>
      latestOpenInNewTab.current?.(path, tabOptions),
    [],
  );
  const treeRowActions = useCallback(
    (path: string) => {
      const file = filesByPath.get(path);
      return file ? menuActionsFor(file) : [];
    },
    [filesByPath, menuActionsFor],
  );

  // The popover's copy also offers closing it; the column's has nothing to close.
  const navigateFromTree = (path: string) => {
    navigateToFile(path);
    onTreeFileChosen?.(path);
  };
  const renderTree = (onClose?: () => void) => (
    <DiffReviewTree
      files={listedFiles}
      navigate={navigateFromTree}
      onOpenInNewTab={onOpenFileInNewTab && openTreeFileInNewTab}
      rowActions={onOpenFileInNewTab || presentation?.treeRowActions ? treeRowActions : undefined}
      folderActions={folderActions}
      folderOwner={folderOwner}
      appearance={presentation?.tree?.appearance}
      searchQuery={treeSearch}
      setSearchQuery={setTreeSearch}
      searchFocusRequest={treeSearchFocusRequest}
      dark={themeType === "dark"}
      onClose={onClose}
    />
  );

  const messages = presentation?.messages;
  const toolbarProps: DiffReviewToolbarProps = {
    commands,
    fileCount: listedFiles.length,
    filters,
    dispatchFilter,
    languages: counts,
    testCount,
    generatedCount,
    filtersOpen,
    setFiltersOpen,
    breakdown,
    languagesOpen: languagesOpen && languagesAvailable,
    setLanguagesOpen,
    reviewActive: commandsActive,
    base,
    extra: source?.toolbar,
    status: source?.toolbarStatus,
    compact: reviewToolbarIsCompact(width),
    treePopover: !wide,
  };

  const codeView = (
    <CodeView
      ref={viewer}
      items={items}
      options={options}
      className="diff-review-scroll h-full"
      style={{
        width: "100%",
        height: "100%",
        overflowY: "scroll",
        scrollbarGutter: "stable",
      }}
      disableWorkerPool={workers.kind === "none"}
      renderHeaderPrefix={renderHeaderPrefix}
      renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
      renderHeaderMetadata={renderHeaderMetadata}
      renderAnnotation={renderAnnotation}
      renderGutterUtility={renderGutterUtility}
      renderCodeViewFooter={renderFooter}
      selectedLines={selectedLines}
      onScroll={handleScroll}
    />
  );
  let body;
  if (files.length === 0) {
    body = (
      <DiffReviewMessage
        title={messages?.empty?.title ?? "No changes"}
        message={messages?.empty?.message ?? emptyMessage}
      />
    );
  } else if (listedFiles.length === 0) {
    if (hasTreeSearch(treeSearch)) {
      const query = treeSearch?.trim() ?? "";
      const text = messages?.noSearchMatches?.(query);
      body = (
        <DiffReviewMessage
          title={text?.title ?? "No matching files"}
          message={text?.message ?? `No changed files match “${query}”.`}
          action={{ label: text?.actionLabel ?? "Clear search", run: () => setTreeSearch(null) }}
        />
      );
    } else {
      const text = messages?.noFilterMatches;
      body = (
        <DiffReviewMessage
          title={text?.title ?? "No matching files"}
          message={text?.message ?? "The filters hide every changed file."}
          action={{
            label: text?.actionLabel ?? "Clear filters",
            run: () => dispatchFilter({ type: "clearFilters" }),
          }}
        />
      );
    }
  } else if (
    messages?.onlyWhitespace &&
    onlyWhitespaceChanged(listedFiles, contents, ignoreWhitespace)
  ) {
    body = (
      <DiffReviewMessage
        title={messages.onlyWhitespace.title}
        message={messages.onlyWhitespace.message}
      />
    );
  } else {
    body =
      workers.kind === "own" ? (
        <WorkerPoolContextProvider
          poolOptions={poolOptions}
          highlighterOptions={highlighterOptions}
        >
          {codeView}
        </WorkerPoolContextProvider>
      ) : workers.kind === "shared" ? (
        <WorkerPoolContext.Provider value={workers.pool}>{codeView}</WorkerPoolContext.Provider>
      ) : (
        codeView
      );
  }

  return (
    <TooltipProvider>
      <Popover open={!wide && narrowTreeOpen} onOpenChange={setNarrowTreeOpen}>
        <div
          ref={setShell}
          data-testid="diff-review"
          data-diff-workers={workers.kind}
          className="flex h-full min-h-0 flex-col"
          style={fontStyle}
        >
          {presentation?.toolbar ? (
            presentation.toolbar(toolbarProps)
          ) : (
            <DiffReviewToolbar {...toolbarProps} />
          )}
          <div ref={bodyRef} className="relative flex min-h-0 flex-1">
            {treeDocked && (
              <div
                data-diff-review-tree-column
                style={{ width: `${treeColumn.width}px` }}
                className="relative shrink-0 border-r border-border"
              >
                <PaneResizeHandle resize={treeColumn} testId="diff-review-tree-resize-handle" />
                {renderTree()}
              </div>
            )}
            <div data-diff-review-content className="relative min-w-0 flex-1">
              {findOpen && (
                <div className="pointer-events-none absolute top-2 right-4 z-20 flex max-w-[calc(100%-2rem)] justify-end">
                  <FindBar
                    label={FIND_IN_CHANGES_LABEL}
                    query={find.query}
                    onQueryChange={find.setQuery}
                    status={find.status}
                    hasMatches={find.hasMatches}
                    searching={find.searching}
                    focusRequest={`${findFocusRequest}:${seededFocus}`}
                    onNext={find.next}
                    onPrevious={find.previous}
                    onClose={onFindClose}
                    options={find.options}
                    onOptionsChange={find.setOptions}
                    testIdPrefix="diff-review-find"
                    footer={
                      find.query && find.hidden.count > 0 && showHiddenCommand ? (
                        <>
                          <span className="min-w-0 flex-1">
                            {hiddenMatchesNote(find.hidden.count, find.hasMatches, {
                              where: "hidden files",
                              truncated: find.hidden.truncated,
                            })}
                          </span>
                          <Button
                            type="button"
                            variant="link"
                            size="xs"
                            className="h-auto shrink-0 px-0 text-xs"
                            onClick={showHiddenCommand.run}
                          >
                            Show
                          </Button>
                        </>
                      ) : undefined
                    }
                  />
                </div>
              )}
              {body}
            </div>
          </div>
        </div>
        <PopoverContent
          align="start"
          collisionPadding={8}
          className={cn("h-96 p-0", treePopoverFullWidth ? "w-[calc(100vw-1rem)]" : "w-72")}
          data-layout={treePopoverFullWidth ? "full-width" : "anchored"}
          aria-label="Changed files"
          // ⌘K and other dialogs act on the tree's focused folder, so the tree stays open for them.
          onInteractOutside={(event) => {
            if (event.target instanceof Element && event.target.closest('[role="dialog"]')) {
              event.preventDefault();
            }
          }}
        >
          {renderTree(() => setNarrowTreeOpen(false))}
        </PopoverContent>
      </Popover>
    </TooltipProvider>
  );
}

function DiffReviewMessage({
  title,
  message,
  action,
}: {
  title: string;
  message: string;
  action?: { label: string; run: () => void };
}) {
  return (
    <div
      role="status"
      className="flex h-full flex-col items-center justify-center gap-2 p-8 text-center text-ui"
    >
      <strong className="font-medium">{title}</strong>
      <span className="text-muted-foreground">{message}</span>
      {action && (
        <Button type="button" variant="outline" size="sm" onClick={action.run}>
          {action.label}
        </Button>
      )}
    </div>
  );
}

/** A Markdown file's switch between its rich and source diff, before its "⋯" menu. */
/** A file's name in its header: its folder quieter, its name bold, then a copy button. */
function ReviewFileTitle({
  path,
  copyPath,
  copyButtonRef,
}: {
  path: string;
  copyPath: DiffReviewFileAction | undefined;
  copyButtonRef?: (button: HTMLButtonElement | null) => void;
}) {
  const nameStart = path.lastIndexOf("/") + 1;
  return (
    <span className="flex min-w-0 items-center gap-1">
      {/* Right-to-left so a long path gives up its folders before its name. */}
      <span className="min-w-0 truncate [direction:rtl]" title={path}>
        <bdi>
          <span className="text-muted-foreground">{path.slice(0, nameStart)}</span>
          <span className="font-semibold text-foreground">{path.slice(nameStart)}</span>
        </bdi>
      </span>
      {copyPath && (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="icon-xs"
              ref={copyButtonRef}
              className="shrink-0 text-muted-foreground @max-[360px]:hidden"
              aria-label={`${copyPath.label} ${path}`}
              // The header toggles its file; copying shouldn't.
              onClick={(event) => {
                event.stopPropagation();
                copyPath.run();
              }}
            >
              <CopyIcon />
            </Button>
          </TooltipTrigger>
          <TooltipContent>{copyPath.label}</TooltipContent>
        </Tooltip>
      )}
    </span>
  );
}

/** Marks a file viewed, folding it away, or clears the mark. */
function ViewedToggle({
  path,
  viewed,
  unavailableReason,
  onToggle,
}: {
  path: string;
  viewed: boolean;
  /** Why the file can't be marked yet; the tooltip explains it. */
  unavailableReason: string | null;
  onToggle: () => void;
}) {
  const available = unavailableReason === null;
  const label = unavailableReason ?? (viewed ? "Viewed" : "Mark as viewed");
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`Viewed ${path}`}
          aria-pressed={viewed}
          aria-disabled={!available || undefined}
          data-state={viewed ? "on" : "off"}
          className="text-muted-foreground aria-disabled:opacity-50 data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
          // The header toggles its file; the mark shouldn't.
          onClick={(event) => {
            event.stopPropagation();
            if (available) onToggle();
          }}
        >
          <CheckIcon />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

/** A file's "Owned by you" badge, which shows only the reader's files or all of them again. */
function OwnedFilesToggle({ command }: { command: DiffReviewCommand }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge
          asChild
          variant="outline"
          className="cursor-pointer border-primary/40 text-primary hover:bg-primary/10 aria-pressed:bg-primary/15 @max-[22rem]:px-1"
        >
          <button
            type="button"
            aria-pressed={command.pressed}
            // The header toggles its file; the filter shouldn't.
            onClick={(event) => {
              event.stopPropagation();
              command.run();
            }}
          >
            <UserCheckIcon aria-hidden />
            {/* A narrow header keeps ownership as an icon: nothing else shows it. */}
            <span className="@max-[22rem]:sr-only">{OWNED_BY_YOU}</span>
          </button>
        </Badge>
      </TooltipTrigger>
      <TooltipContent>{command.label}</TooltipContent>
    </Tooltip>
  );
}

function RichDiffToggle({ path, action }: { path: string; action: MarkdownDiffViewAction }) {
  const Icon = BookOpenTextIcon;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`Rich diff of ${path}`}
          aria-pressed={action.rich}
          data-state={action.rich ? "on" : "off"}
          className="data-[state=on]:bg-accent data-[state=on]:text-accent-foreground"
          // The header toggles its file; the switch shouldn't.
          onClick={(event) => {
            event.stopPropagation();
            action.run();
          }}
        >
          <Icon />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{action.label}</TooltipContent>
    </Tooltip>
  );
}

/** A rich file's preview row: asks for the preview once it is on screen. */
function MarkdownPreviewSlot({
  path,
  fileDiff,
  markdown,
  setRich,
  onResize,
  hasSourceComments,
}: {
  path: string;
  fileDiff: FileDiffMetadata;
  markdown: MarkdownPreviews;
  /** Switches the file's view, keeping it in place. */
  setRich: (path: string, rich: boolean) => void;
  /** The preview resized: a new width may need the reader's place put back. */
  onResize: (path: string, width: number, version: string | null) => void;
  hasSourceComments: boolean;
}) {
  const { request, retry, states } = markdown;
  const onRequest = useCallback(() => request(path, fileDiff), [request, path, fileDiff]);
  const onRetry = useCallback(() => retry(path), [retry, path]);
  const onShowSource = useCallback(() => setRich(path, false), [setRich, path]);
  const onPreviewResize = useCallback(
    (width: number, version: string | null) => onResize(path, width, version),
    [onResize, path],
  );
  return (
    <MarkdownDiffPreview
      onResize={onPreviewResize}
      path={path}
      state={states.get(path)}
      onRequest={onRequest}
      onRetry={onRetry}
      onShowSource={onShowSource}
      hasSourceComments={hasSourceComments}
    />
  );
}

/** Why a file shows no diff, or that its diff is out of date, in the file's header. */
/** A file header's "⋯" menu of per-file actions. */
/**
 * The listed file the reader is at: `path` when it's listed, else the next
 * listed file after it in review order (or the last), and none when nothing is.
 */
export function listedFileAt(
  path: string | null,
  files: readonly DiffReviewFile[],
  listed: readonly DiffReviewFile[],
): DiffReviewFile | undefined {
  if (listed.length === 0) return undefined;
  const exact = listed.find((file) => file.path === path);
  if (exact) return exact;
  const position = files.findIndex((file) => file.path === path);
  if (position < 0) return listed[0];
  const order = new Map(files.map((file, index) => [file.path, index]));
  return (
    listed.find((file) => (order.get(file.path) ?? -1) > position) ?? listed[listed.length - 1]
  );
}

/** Whether `element` is laid out and shown, as a hidden container-query rule leaves it not. */
function isShown(element: HTMLElement): boolean {
  if (typeof element.checkVisibility === "function") return element.checkVisibility();
  return getComputedStyle(element).display !== "none";
}

function FileActionsMenu({
  path,
  actions,
  titleShowsCopy,
}: {
  path: string;
  actions: readonly DiffReviewFileAction[];
  titleShowsCopy: (path: string) => boolean;
}) {
  // Read when the menu opens: the header's width decides whether the title shows the copy button.
  const [copyInTitle, setCopyInTitle] = useState(false);
  if (actions.length === 0) return null;
  const others = actions.filter((action) => action.id !== "copy-path");
  const menuActions = copyInTitle && others.length > 0 ? others : actions;
  return (
    <DropdownMenu onOpenChange={(open) => open && setCopyInTitle(titleShowsCopy(path))}>
      <DropdownMenuTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          aria-label={`More actions for ${path}`}
          // The header toggles its file; the menu shouldn't.
          onClick={(event) => event.stopPropagation()}
        >
          <MoreHorizontalIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {menuActions.map(({ id, label, icon: Icon, disabledReason, link, run }) => (
          <SessionActionMenuItem
            key={id}
            disabledReason={disabledReason}
            link={link}
            onSelect={run}
          >
            <Icon />
            {label}
          </SessionActionMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Why a file's diff needs a retry: its load failed, or its refresh did and the diff is out of date. */
function retryReason(content: DiffReviewContent | undefined): string | null {
  if (content?.status === "failed") return content.message;
  if (content?.status === "ready" && content.refreshError) {
    return `Showing an earlier version. ${content.refreshError}`;
  }
  return null;
}

/** Retries a file's diff; its tooltip gives the reason, which a narrow header leaves out. */
function RetryFileButton({
  content,
  onRetry,
}: {
  content: DiffReviewContent | undefined;
  onRetry: () => void;
}) {
  const reason = retryReason(content);
  if (reason == null) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          // A narrow header keeps only the icon, in the failure's colour.
          className={
            content?.status === "failed"
              ? "@max-[22rem]:text-destructive"
              : "@max-[22rem]:text-warning"
          }
          onClick={(event) => {
            event.stopPropagation();
            onRetry();
          }}
        >
          <RotateCwIcon />
          <span className="@max-[22rem]:sr-only">Try again</span>
        </Button>
      </TooltipTrigger>
      {/* The header sits at the pane's edge; keep a long reason clear of the window's. */}
      <TooltipContent collisionPadding={8}>{reason}</TooltipContent>
    </Tooltip>
  );
}

/** A file header's note; it narrows with the header (its container) so the name and actions stay. */
function FileStatusNote({
  file,
  content,
}: {
  file: DiffReviewFile;
  content: DiffReviewContent | undefined;
}) {
  if (file.isBinary) return null;
  if (content?.status === "ready") {
    const reason = retryReason(content);
    if (reason == null) return null;
    return (
      <span
        data-testid="diff-review-file-status"
        title={reason}
        className="max-w-[min(18rem,30cqw)] truncate text-warning @max-[22rem]:sr-only"
      >
        {reason}
      </span>
    );
  }
  const label = placeholderLabel(file, content);
  if (content?.status === "failed") {
    // A narrow header gives way to the name: a failure is still announced, beside its red retry icon.
    return (
      <span
        data-testid="diff-review-file-status"
        role="alert"
        title={label}
        className="max-w-[min(18rem,30cqw)] truncate text-destructive @max-[22rem]:sr-only"
      >
        {label}
      </span>
    );
  }
  // A narrow header keeps the note as an icon, named by its text, that shows it in full when tapped.
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="diff-review-file-status"
          title={label}
          onClick={(event) => event.stopPropagation()}
          className="flex max-w-[min(18rem,30cqw)] min-w-0 cursor-default items-center rounded-sm text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none @max-[22rem]:cursor-pointer @max-[22rem]:hover:text-foreground"
        >
          <InfoIcon aria-hidden className="hidden size-3.5 shrink-0 @max-[22rem]:block" />
          <span className="truncate @max-[22rem]:sr-only">{label}</span>
        </button>
      </PopoverTrigger>
      {/* The header sits at the pane's edge; keep the note clear of the window's. */}
      <PopoverContent collisionPadding={8} className="w-auto max-w-64 p-2 text-xs">
        {label}
      </PopoverContent>
    </Popover>
  );
}

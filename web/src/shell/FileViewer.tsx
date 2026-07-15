// Right-panel file viewer with Shiki syntax highlighting and a comments side panel.
//
// Layout:
//   ┌─────────────────────────────────────────────────────┐
//   │ Header: [← close] · breadcrumb · [💬] · [Δ] · [⤓] │
//   ├──────────────────────────────────┬──────────────────┤
//   │  Code / diff viewer (scrollable) │  CommentsPanel   │
//   │  - Shiki highlighted             │  (toggleable)    │
//   │  - gutter icon → add comment    │                  │
//   └──────────────────────────────────┴──────────────────┘

import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  AlertTriangleIcon,
  ArrowLeftIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CloudOffIcon,
  CodeIcon,
  Columns2Icon,
  DownloadIcon,
  EyeIcon,
  EyeOffIcon,
  FileDiffIcon,
  Link2Icon,
  Loader2Icon,
  MessageSquareTextIcon,
  MoreHorizontalIcon,
  PencilLineIcon,
  RowsIcon,
  SearchIcon,
  SquareArrowOutUpRightIcon,
  Trash2Icon,
  WrapTextIcon,
} from "lucide-react";
import { useSearchParams } from "@/lib/routing";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { fileContentToBlob, triggerBrowserDownload, useFileContent } from "@/hooks/useFileContent";
import { useFileDiff } from "@/hooks/useFileDiff";
import {
  type Comment,
  useAddComment,
  useComments,
  useDeleteComment,
  useUpdateComment,
} from "@/hooks/useComments";
import { CommentSenderProvider, useOptionalCommentSender } from "@/hooks/CommentSenderContext";
import { markCommentsSeen } from "@/hooks/useSeenComments";
import { useChatStore } from "@/store/chatStore";
import { useResizablePanel } from "@/hooks/useResizablePanel";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { cn } from "@/lib/utils";
import { readFileViewPreferences, writeFileViewPreferences } from "@/lib/fileViewPreferences";
import { type ChangedSort, compareChangedFiles } from "./FlatFileList";
import { CodeViewer } from "./CodeViewer";
import {
  MONACO_SPLIT_BREAKPOINT,
  type SaveStatus,
  detectLang,
  isImageFile,
  isNotebookPath,
  isPdfFile,
  openHtmlArtifactInNewTab,
} from "./codeViewerHelpers";
import { CommentsPanel, type ActiveSelection } from "./CommentsPanel";

// Monaco diff is heavy (~MBs + worker); load it only when the diff view is
// actually shown.
const MonacoDiffViewer = lazy(() =>
  import("./MonacoDiffViewer").then((m) => ({ default: m.MonacoDiffViewer })),
);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Classify comments into open/addressed and remap open draft comments to
 * their correct absolute offsets when the file content has changed.
 *
 * - **open**: draft comment — kept with updated offsets if the anchor
 *   moved, or at stored offsets if the anchor is no longer present
 *   (never silently dropped).
 * - **addressed**: handled by the agent or user (status "addressed").
 */
export function classifyAndRemapComments(
  comments: Comment[],
  fileContent: string,
): { open: Comment[]; addressed: Comment[] } {
  const open: Comment[] = [];
  const addressed: Comment[] = [];

  for (const c of comments) {
    if (c.status === "addressed") {
      addressed.push(c);
      continue;
    }
    // Draft with no anchor — keep as-is.
    if (!c.anchor_content) {
      open.push(c);
      continue;
    }
    // File not yet loaded — keep at stored offsets rather than dropping.
    if (!fileContent) {
      open.push(c);
      continue;
    }
    // Search near the stored offset first to handle the common case of a
    // small edit before the anchor (insertion/deletion shifts the offset by
    // a few characters). Only fall back to a global search if not found
    // nearby, to avoid remapping to a different occurrence of the same text.
    const SEARCH_WINDOW = 200;
    const windowStart = Math.max(0, c.start_index - SEARCH_WINDOW);
    const windowEnd = Math.min(
      fileContent.length,
      c.start_index + c.anchor_content.length + SEARCH_WINDOW,
    );
    const nearbyIdx = fileContent.indexOf(c.anchor_content, windowStart);
    const idx =
      nearbyIdx !== -1 && nearbyIdx <= windowEnd
        ? nearbyIdx
        : fileContent.indexOf(c.anchor_content);
    if (idx === -1) {
      // Anchor not found anywhere — keep at stored offsets rather than dropping.
      open.push(c);
      continue;
    }
    if (idx !== c.start_index) {
      // Text moved — update offsets.
      open.push({ ...c, start_index: idx, end_index: idx + c.anchor_content.length });
    } else {
      open.push(c);
    }
  }

  return { open, addressed };
}

/** Floor (px) always reserved for the file-path label, so even a 1-char name
 * keeps a little breathing room before the toolbar collapses. */
const TOOLBAR_MIN_TITLE_PX = 48;
/** Ceiling (px) on the path reserve. A very long path is `truncate`d, so past
 * this we stop letting it push the toolbar into the overflow menu — otherwise a
 * deeply-nested filename would collapse the buttons even on a wide panel. */
const TOOLBAR_MAX_TITLE_PX = 280;

/**
 * Decide whether a header's inline toolbar buttons fit, or must collapse into a
 * single overflow ("⋯") menu.
 *
 * The decision is a function of ONE live variable — the header's available
 * width — compared against a width requirement assembled from
 * *state-independent* measurements:
 *
 *   collapsed  ⇔  headerWidth  <  backWidth + navWidth + titleReserve + chipWidth + buttonsWidth + gaps
 *
 * Each piece is measured from something whose value does NOT change when the
 * toggle flips, which is what keeps the comparison monotonic (collapse and
 * expand at the same threshold, never stuck):
 *   - `backRef`    → mobile close/back button (`shrink-0`, constant; 0 in
 *                    frameless desktop mode where it's absent).
 *   - `navRef`     → prev/next cluster (`shrink-0`, constant; 0 when absent).
 *   - `chipRef`    → save-status chip (`shrink-0`, constant; 0 when idle).
 *   - `measureRef` → OFFSCREEN clone of the full button row (never reflows).
 *   - `pathMeasureRef` → an OFFSCREEN, unconstrained clone of the path label.
 *                    Its `offsetWidth` is the label's intrinsic text width,
 *                    independent of the live layout. The reserve is that width
 *                    clamped to [MIN, MAX], so a longer filename collapses the
 *                    toolbar earlier than a short one (path-aware), while an
 *                    absurdly long path can't collapse it on a wide panel.
 *
 * NOTE: we must NOT measure the live `flex-1` path span — neither `offsetWidth`
 * (its rendered box grows to fill space freed by collapsing) nor `scrollWidth`
 * (which equals the box width once the box exceeds the text) is stable across
 * the toggle, so either would move the threshold and the toggle would get
 * stuck. The offscreen clone has no such coupling.
 */
function useToolbarOverflow(actionsKey: string): {
  headerRef: React.RefObject<HTMLDivElement | null>;
  backRef: React.RefObject<HTMLDivElement | null>;
  navRef: React.RefObject<HTMLDivElement | null>;
  pathMeasureRef: React.RefObject<HTMLSpanElement | null>;
  chipRef: React.RefObject<HTMLSpanElement | null>;
  measureRef: React.RefObject<HTMLDivElement | null>;
  collapsed: boolean;
} {
  const headerRef = useRef<HTMLDivElement | null>(null);
  const backRef = useRef<HTMLDivElement | null>(null);
  const navRef = useRef<HTMLDivElement | null>(null);
  const pathMeasureRef = useRef<HTMLSpanElement | null>(null);
  const chipRef = useRef<HTMLSpanElement | null>(null);
  const measureRef = useRef<HTMLDivElement | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  useLayoutEffect(() => {
    const header = headerRef.current;
    const measure = measureRef.current;
    if (!header || !measure || typeof ResizeObserver === "undefined") return;
    const evaluate = () => {
      const style = getComputedStyle(header);
      const padX = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight);
      const available = header.clientWidth - padX;
      if (available <= 0) return; // not laid out yet (or jsdom) — keep current state.

      const backWidth = backRef.current?.offsetWidth ?? 0;
      const navWidth = navRef.current?.offsetWidth ?? 0;
      const chipWidth = chipRef.current?.offsetWidth ?? 0;
      const buttonsWidth = measure.scrollWidth;
      // Path-aware reserve: the label's intrinsic text width, clamped. Measured
      // from an OFFSCREEN, unconstrained clone of the label — NOT the live
      // `flex-1` span, whose scrollWidth would equal its (variable) box width
      // once the box grows wider than the text, breaking monotonicity.
      const pathNatural = pathMeasureRef.current?.offsetWidth ?? 0;
      const titleReserve = Math.min(
        Math.max(pathNatural, TOOLBAR_MIN_TITLE_PX),
        TOOLBAR_MAX_TITLE_PX,
      );
      // ~12px covers the inter-group flex gaps.
      const required = backWidth + navWidth + titleReserve + chipWidth + buttonsWidth + 12;
      setCollapsed(available < required);
    };
    evaluate();
    // Observe only the header (the sole live variable). Content changes (action
    // set / chip / nav / path per file) are picked up by the effect re-running
    // on `actionsKey`, which calls evaluate() once.
    const ro = new ResizeObserver(evaluate);
    ro.observe(header);
    return () => ro.disconnect();
  }, [actionsKey]);

  return { headerRef, backRef, navRef, pathMeasureRef, chipRef, measureRef, collapsed };
}

// ---------------------------------------------------------------------------
// FileViewer
// ---------------------------------------------------------------------------

interface FileViewerProps {
  open: boolean;
  conversationId: string;
  path: string;
  onClose: () => void;

  /** Called when the user navigates to a different file via the prev/next buttons. */
  onNavigateTo?: (path: string) => void;
  /**
   * Numeric permission level for the current user on this session
   * (1 = read, 2 = edit, 3 = manage, 4 = owner). ``null`` means
   * single-user mode (no restrictions enforced).
   */
  permissionLevel?: number | null;
  /**
   * Frameless mode: renders as a flex-1 div filling the parent instead
   * of a push-panel aside with its own width and slide animation. Used
   * when the viewer is embedded inside the inline right panel.
   */
  frameless?: boolean;
  /** Called when the user presses Escape to close the active file tab. */
  onCloseTab?: () => void;
  /** Called when the comments panel opens or closes inside the viewer. */
  onCommentsOpenChange?: (open: boolean) => void;
  /**
   * Sort order for the prev/next navigation. Must match the order the
   * Changes list (`FilesPanel` → `FlatFileList`) is sorted by, or the
   * "X/N" index won't line up with the list. Defaults to "recent".
   */
  sort?: ChangedSort;
}

/**
 * Outer wrapper that resolves the agentId and provides a
 * `CommentSenderContext`. The body lives in `FileViewerBody`, which
 * reads the (possibly `null`) sender via `useOptionalCommentSender`.
 *
 * This keeps the "no agent registered" fallback inside the provider.
 */
export function FileViewer(props: FileViewerProps) {
  // boundAgentId is populated from the active session's agent binding —
  // works for both template agents and session-scoped agents from
  // `omnigent run --server`.
  const agentId = useChatStore((s) => s.boundAgentId);
  return (
    <CommentSenderProvider sessionId={props.conversationId} agentId={agentId}>
      <FileViewerBody {...props} />
    </CommentSenderProvider>
  );
}

function FileViewerBody({
  open,
  conversationId,
  path,
  onClose,
  onCloseTab,
  onNavigateTo,
  permissionLevel,
  frameless,
  onCommentsOpenChange,
  sort = "recent",
}: FileViewerProps) {
  // null = single-user mode (no enforcement); undefined = prop not provided (treat as unrestricted).
  // LEVEL_EDIT = 2; levels below 2 are read-only.
  const canEdit = permissionLevel == null || permissionLevel >= 2;
  const [searchParams, setSearchParams] = useSearchParams();
  // Capture URL params once on open — we don't want re-renders caused by our own
  // param writes to re-run the initialization logic.
  const initialDiffRef = useRef(searchParams.get("diff") === "1");
  const initialCommentIdRef = useRef(searchParams.get("comment"));
  // Seeded from the parent's persisted state on remount (e.g. returning to a
  // tab); defaults closed on a fresh open. The linked-comment / fresh-open
  // effects below only force it open, never closed, so a restored-open value
  // survives and a restored-closed value can still be opened by ?comment=.
  const [commentsOpen, setCommentsOpen] = useState(false);
  // In frameless mode the panel is embedded in the parent aside — no own
  // width or slide animation. Still call the hook unconditionally to
  // satisfy Rules of Hooks; just pass false so it never animates.
  // When the comments panel is open it takes up w-60 (240px), so require
  // enough width for both the code viewer (320px) and the comments panel.
  const COMMENTS_PANEL_WIDTH_PX = 240; // Tailwind w-60
  const minWidthPx = commentsOpen ? 480 + COMMENTS_PANEL_WIDTH_PX : undefined;
  const { panelWidth, handleProps, isDesktop } = useResizablePanel(
    frameless ? false : open,
    50,
    frameless ? undefined : minWidthPx,
  );
  const fileQuery = useFileContent(conversationId, path);
  const diffQuery = useFileDiff(conversationId, path);
  const changedFiles = useWorkspaceChangedFiles(conversationId);

  // Build the navigable file list from all changed files (including deleted),
  // sorted the same way FilesPanel sorts its flat view so the "X/N" index
  // matches the Changes list position. Memoized so the sort runs only when the
  // changed-files list or sort order changes, not on every viewer re-render.
  const navigableFiles = useMemo(
    () => [...(changedFiles.data?.data ?? [])].sort(compareChangedFiles(sort)).map((f) => f.path),
    [changedFiles.data?.data, sort],
  );
  const currentNavIdx = navigableFiles.indexOf(path);
  const prevPath = currentNavIdx > 0 ? navigableFiles[currentNavIdx - 1] : null;
  const nextPath =
    currentNavIdx >= 0 && currentNavIdx < navigableFiles.length - 1
      ? navigableFiles[currentNavIdx + 1]
      : null;
  const commentsQuery = useComments(conversationId, path);
  const addComment = useAddComment(conversationId);
  const updateComment = useUpdateComment(conversationId);
  const deleteComment = useDeleteComment(conversationId);
  const commentsInitializedRef = useRef(false);
  const linkedCommentAppliedRef = useRef(false);
  const viewModeInitializedRef = useRef(false);
  const prevOpenRef = useRef(open);
  const [activeSelection, setActiveSelection] = useState<ActiveSelection | null>(null);
  // Measured width of the code/diff content area. Drives whether the
  // split/unified toggle is offered: split is only usable at >= the Monaco
  // breakpoint, so below it we hide the toggle to avoid a no-op control.
  // null = not yet measured (or zero, e.g. jsdom) — treat as "wide enough" so
  // the toggle shows by default and only hides once a real narrow width lands.
  const contentAreaRef = useRef<HTMLDivElement | null>(null);
  const [contentWidth, setContentWidth] = useState<number | null>(null);
  // Tracks the in-progress comment textarea body. Used by MarkdownCommentPlugin
  // to decide whether to preserve the pending mark when the user clicks away.
  const pendingBodyRef = useRef("");
  const [isEditorDirty, setIsEditorDirty] = useState(false);
  // Auto-save lifecycle reported up from the Monaco editor, shown as a status
  // chip in the toolbar (the editor itself no longer has a Save button).
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  // Brief "copied" feedback for the copy-link button.
  const [linkCopied, setLinkCopied] = useState(false);
  const linkCopiedTimerRef = useRef<number>(0);
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);
  // Reset selection state whenever the file changes.
  useEffect(() => {
    setActiveSelection(null);
    setIsEditorDirty(false);
    setSaveStatus("idle");
  }, [path]);
  // Reset comments initialization when the viewer transitions from closed to open,
  // so the panel state is derived from the freshly-opened file's comments.
  // When navigating via < > arrows (path changes while already open), the
  // initialized flag remains set, preserving whatever panel state the user set.
  useEffect(() => {
    if (open && !prevOpenRef.current) {
      commentsInitializedRef.current = false;
    }
    prevOpenRef.current = open;
  }, [open]);
  // Mark the panel as initialized once comment data arrives so re-navigating
  // between files doesn't reset the user's manual open/close choice.
  useEffect(() => {
    if (!open || commentsInitializedRef.current) return;
    if (commentsQuery.data === undefined) return;
    commentsInitializedRef.current = true;
  }, [open, commentsQuery.data]);
  // Comments count as seen only while the comments panel is OPEN on
  // this file — merely opening the file (markers in the gutter, panel
  // collapsed) must NOT clear them from the Inbox page / sidebar badge
  // (`useCommentInbox`). The panel opens via the toggle button, a
  // gutter-marker click / text selection, or an inbox "Open file"
  // deep link (?comment= auto-opens it) — all moments the comment
  // bodies are actually on screen. Re-runs whenever the comment list
  // refreshes, so a comment arriving while the panel is open is
  // marked seen too.
  useEffect(() => {
    if (!open || !commentsOpen || commentsQuery.data === undefined) return;
    markCommentsSeen(commentsQuery.data.map((c) => c.id));
  }, [open, commentsOpen, commentsQuery.data]);
  // Notify parent when comments panel opens or closes (e.g. so the parent
  // can widen the inline panel to fit both the code viewer and comments).
  useEffect(() => {
    onCommentsOpenChange?.(commentsOpen);
  }, [commentsOpen, onCommentsOpenChange]);
  // Warn on browser close/refresh while there are unsaved changes.
  useEffect(() => {
    if (!isEditorDirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isEditorDirty]);

  const guardDirty = useCallback(
    (action: () => void) => {
      if (isEditorDirty) {
        setPendingAction(() => action);
        return;
      }
      action();
    },
    [isEditorDirty],
  );

  const handleSetActiveSelection = (sel: ActiveSelection | null) => {
    setActiveSelection(sel);
    if (sel !== null) {
      commentsInitializedRef.current = true;
      setCommentsOpen(true);
    }
  };

  useEffect(
    () => () => {
      window.clearTimeout(linkCopiedTimerRef.current);
    },
    [],
  );

  const downloadFile = useCallback(() => {
    const data = fileQuery.data;
    if (!data) return;
    triggerBrowserDownload(fileContentToBlob(data), path.split("/").pop() ?? path);
  }, [fileQuery.data, path]);

  // Pop the HTML artifact into its own browser tab. The artifact is rendered in
  // a sandboxed, opaque-origin iframe (see `openHtmlArtifactInNewTab`), so it
  // stays isolated from the host app — full-window rendering, no origin sharing.
  const openHtmlInNewTab = useCallback(() => {
    const data = fileQuery.data;
    if (!data) return;
    const opened = openHtmlArtifactInNewTab(data.content, path.split("/").pop() ?? path);
    if (!opened) {
      // window.open returned null — almost always a popup blocker. There's no
      // toast surface here, so log it rather than failing silently.
      console.warn("Open in new tab: the browser blocked the popup window.");
    }
  }, [fileQuery.data, path]);

  const copyFileLink = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    const url = new URL(window.location.href);
    if (!activeSelection) url.searchParams.delete("comment");
    navigator.clipboard.writeText(url.toString()).then(
      () => {
        setLinkCopied(true);
        window.clearTimeout(linkCopiedTimerRef.current);
        linkCopiedTimerRef.current = window.setTimeout(() => setLinkCopied(false), 2000);
      },
      (err) => console.warn("Failed to copy file link", err),
    );
  }, [activeSelection]);

  const copyCommentLink = useCallback((commentId: string) => {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return;
    const url = new URL(window.location.href);
    url.searchParams.set("comment", commentId);
    navigator.clipboard
      .writeText(url.toString())
      .then(undefined, (err) => console.warn("Failed to copy comment link", err));
  }, []);

  const sender = useOptionalCommentSender();

  const allComments = useMemo(() => commentsQuery.data ?? [], [commentsQuery.data]);
  const fileContent = useMemo(() => fileQuery.data?.content ?? "", [fileQuery.data]);
  const { open: openComments, addressed: addressedComments } = useMemo(
    () => classifyAndRemapComments(allComments, fileContent),
    [allComments, fileContent], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // Apply the linked comment (from ?comment= URL param) once per lifecycle.
  // Waits for fileQuery.data so classifyAndRemapComments has run with real content,
  // ensuring activeSelection uses remapped indices that match openComments.
  useEffect(() => {
    if (linkedCommentAppliedRef.current) return;
    const commentId = initialCommentIdRef.current;
    if (!commentId || fileQuery.data === undefined) return;
    const comment = openComments.find((c) => c.id === commentId);
    if (!comment) return;
    linkedCommentAppliedRef.current = true;
    commentsInitializedRef.current = true;
    setCommentsOpen(true);
    setActiveSelection({
      start_index: comment.start_index,
      end_index: comment.end_index,
      anchor_content: comment.anchor_content ?? "",
    });
  }, [openComments]); // eslint-disable-line react-hooks/exhaustive-deps

  // Find-in-file state lifted here so the toolbar button can open it.
  const [searchOpen, setSearchOpen] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const openSearch = useCallback(() => {
    setSearchOpen((prev) => {
      if (prev) return false;
      setTimeout(() => searchInputRef.current?.focus(), 0);
      return true;
    });
  }, [searchInputRef]);

  // Keyboard shortcut: Alt+← / Alt+→ to navigate between changed files.
  useEffect(() => {
    if (!open || !onNavigateTo || currentNavIdx === -1) return;
    const handler = (e: KeyboardEvent) => {
      if (!e.altKey) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      // Don't hijack word-navigation when the user is typing in an input.
      const target = e.target;
      if (
        target instanceof HTMLElement &&
        target.closest('textarea, input, [contenteditable="true"]')
      ) {
        return;
      }
      if (e.key === "ArrowLeft" && prevPath) {
        e.preventDefault();
        guardDirty(() => onNavigateTo(prevPath));
      } else if (e.key === "ArrowRight" && nextPath) {
        e.preventDefault();
        guardDirty(() => onNavigateTo(nextPath));
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onNavigateTo, currentNavIdx, prevPath, nextPath, guardDirty]);

  // Escape closes the active file tab (when search is not open).
  useEffect(() => {
    if (!open || !onCloseTab) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (searchOpen) return;
      const target = e.target;
      if (
        target instanceof HTMLElement &&
        target.closest('textarea, input, [contenteditable="true"]')
      ) {
        return;
      }
      e.preventDefault();
      guardDirty(onCloseTab);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [open, onCloseTab, searchOpen, guardDirty]);

  // View mode toggle — markdown defaults to the rich-text editor, HTML and
  // notebooks to their rendered preview, and everything else to source.
  const lang = detectLang(path);
  const isPreviewable = lang === "markdown" || lang === "html" || isNotebookPath(path);
  // Images and PDFs render through CodeViewer's own viewers regardless of view
  // mode; they have no source/diff representation, so diff is suppressed for
  // them (Monaco would otherwise render the base64 payload as garbage text).
  const isImage = isImageFile(path, fileQuery.data?.content_type);
  const isPdf = isPdfFile(path, fileQuery.data?.content_type);
  // Show Δ button only when the file appears in the session's changed-files list.
  const isDiffAvailable =
    !isImage && !isPdf && (changedFiles.data?.data.some((f) => f.path === path) ?? false);
  const isDeletedFile =
    changedFiles.data?.data.some((f) => f.path === path && f.status === "deleted") ?? false;

  // Diff is a global toggle — turning it on/off on any file carries over as you
  // navigate to the next file. Source ↔ preview is also shared across previewable
  // files (markdown/html/notebooks), while non-previewable files always render
  // as source.
  // These are app-global *preferences*, persisted to localStorage so they also
  // survive a page refresh (and seed a brand-new conversation). Seed precedence:
  //   1. an explicit ?diff=1 link (shareable override, diff only),
  //   2. the persisted preference,
  //   3. the hardcoded default.
  // Read once on mount so our own writes (and within-tab file navigation) don't
  // re-run the initializers.
  const persistedPrefsRef = useRef(readFileViewPreferences());
  const [diffActive, setDiffActive] = useState(
    () => initialDiffRef.current || persistedPrefsRef.current.diffActive,
  );
  const [diffLayout, setDiffLayout] = useState<"unified" | "split">(
    () => persistedPrefsRef.current.diffLayout,
  );
  const [hideWhitespace, setHideWhitespace] = useState(
    () => persistedPrefsRef.current.hideWhitespace,
  );
  const [wrapLines, setWrapLines] = useState(() => persistedPrefsRef.current.wrapLines);
  const [previewableViewMode, setPreviewableViewMode] = useState<"editor" | "preview" | "source">(
    () => persistedPrefsRef.current.previewableViewMode,
  );
  // A ?comment= deep link to a markdown file must open on the rich-text editor
  // so the comment's anchor highlight is visible in context — the whole point
  // of following the link. The editor is forced regardless of the user's sticky
  // preference: the read-only Preview can't render the highlight at all, so a
  // Preview-preferring user would otherwise land on a surface where the comment
  // they came to see isn't shown. The bias is dropped the moment the user picks
  // a mode themselves, and never applies to any other file.
  //
  // This is a separate override rather than a seeded `previewableViewMode`
  // because that state is persisted globally: seeding it to "editor" would write
  // "editor" back to localStorage, clobbering the user's own preference for
  // every later markdown file. It must also be reactive — flipping this override
  // is what re-renders to the chosen surface once the user picks a mode. It's
  // the deep-linked path (not a boolean) so a navigate-away-and-back doesn't
  // re-trigger the bias on the wrong file.
  const [deepLinkBiasPath, setDeepLinkBiasPath] = useState<string | null>(() =>
    initialCommentIdRef.current ? path : null,
  );

  // Persist the global view preferences so they survive a refresh. commentsOpen
  // is intentionally excluded — it's contextual (per-open), not a sticky
  // preference. Idempotent on mount (writes back the seeded values).
  useEffect(() => {
    writeFileViewPreferences({
      diffActive,
      diffLayout,
      previewableViewMode,
      hideWhitespace,
      wrapLines,
    });
  }, [diffActive, diffLayout, previewableViewMode, hideWhitespace, wrapLines]);
  // Markdown supports all three previewable modes (preview / editor / source).
  // HTML and notebooks have no rich-text editor, so their "editor" preference
  // falls back to the rendered preview; "preview" / "source" pass through. The shared
  // preference still carries across file types — opening markdown in source
  // then switching to an HTML file keeps you in source, etc.
  const fileViewMode: "editor" | "preview" | "source" = isPreviewable
    ? lang === "markdown"
      ? deepLinkBiasPath === path
        ? "editor"
        : previewableViewMode
      : previewableViewMode === "editor"
        ? "preview"
        : previewableViewMode
    : "source";
  // Derived effective view mode — diff takes priority when active and available.
  const viewMode: "editor" | "preview" | "source" | "diff" =
    diffActive && isDiffAvailable ? "diff" : fileViewMode;
  const diffViewActive = viewMode === "diff";
  // Measure the content area so the split toggle can hide when there isn't
  // enough room for side-by-side. Only observe while the diff is shown — the
  // ref element only exists then, and it's the only mode that cares.
  useEffect(() => {
    const el = contentAreaRef.current;
    if (!el || !diffViewActive || typeof ResizeObserver === "undefined") return;
    const measure = () => setContentWidth(el.getBoundingClientRect().width);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [diffViewActive]);
  // A measured width of 0 (pre-layout, or jsdom) is indistinguishable from
  // "unknown", so treat null/zero as wide enough — only a real sub-threshold
  // measurement hides the toggle, so it never flickers off before layout.
  const splitToggleAvailable =
    contentWidth === null || contentWidth === 0 || contentWidth >= MONACO_SPLIT_BREAKPOINT;
  useEffect(() => {
    if (viewMode !== "editor") setIsEditorDirty(false);
    // Skip on mount — only clear when the user actively switches modes.
    // Clearing on mount would race with the linked-comment effect.
    if (viewModeInitializedRef.current && (viewMode === "editor" || viewMode === "preview")) {
      setActiveSelection(null);
    }
    viewModeInitializedRef.current = true;
  }, [viewMode]);

  // Sync diff state to URL. Skip when already in sync to avoid clobbering ?file=
  // that AppShell writes (React Router v7 BrowserRouter defers via startTransition,
  // so stale searchParams seen here could emit a navigate("?") that strips it).
  useEffect(() => {
    if (!open) return;
    const wantDiff = diffActive && isDiffAvailable;
    const hasDiff = searchParams.has("diff");
    if (wantDiff === hasDiff) return; // already in sync — no navigate needed
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (wantDiff) {
          next.set("diff", "1");
        } else {
          next.delete("diff");
        }
        return next;
      },
      { replace: true },
    );
  }, [diffActive, isDiffAvailable, open]); // eslint-disable-line react-hooks/exhaustive-deps

  // Toolbar actions, declared once and rendered two ways: inline icon buttons
  // when there's room, or rows in an overflow ("⋯") menu when there isn't.
  // `active` drives the inline button's filled variant; it's omitted from the
  // dropdown rows (menu items aren't toggles). The save-status chip is NOT in
  // this list — it stays inline regardless of width.
  //
  // An action can instead carry `options`: a set of mutually-exclusive choices
  // rendered as a single dropdown (a "picker" button inline, a submenu when
  // collapsed) rather than one button per choice. Markdown's view-mode picker
  // (Preview / Edit / Source) uses this so it occupies one toolbar slot.
  type ToolbarOption = {
    key: string;
    label: string;
    tooltip?: string;
    icon: ReactNode;
    onSelect: () => void;
    active: boolean;
    /** Keep the menu open after selecting — for toggles the user may flip in a
     * row (e.g. wrap + whitespace). Action items (Find) omit it so the menu
     * closes as they hand off. */
    keepOpen?: boolean;
    /** Suppress the active check mark — for toggles whose icon already reflects
     * state (e.g. the whitespace eye flips open/closed). */
    noActiveCheck?: boolean;
  };
  type ToolbarAction = {
    key: string;
    /** Accessible name for the inline icon button. */
    label: string;
    /** Tooltip + dropdown row text; falls back to `label` when omitted. */
    tooltip?: string;
    icon: ReactNode;
    onSelect?: () => void;
    active?: boolean;
    /** When set, render a picker (dropdown/submenu) over these mutually
     * exclusive choices instead of a single button. `onSelect` is ignored. */
    options?: ToolbarOption[];
    /** When set, render a menu of independent items (toggles + actions) under a
     * single trigger. Unlike `options`, these are not mutually exclusive and
     * carry no "selected choice" semantics. `onSelect` is ignored. */
    menu?: ToolbarOption[];
  };
  const toolbarActions: ToolbarAction[] = [];
  if (lang === "markdown" && viewMode !== "diff") {
    // Markdown is a segmented control over three reachable modes: the rich-text
    // Editor (default), the rendered Preview, and raw Source. Switching away
    // from the editor must guard unsaved edits; the read-only preview/source
    // surfaces carry no edits, so they switch freely.
    const switchTo = (mode: "preview" | "editor" | "source") => {
      // No-op when already on this surface — re-selecting the active tab must
      // not run the dirty guard (which would pop a discard dialog for nothing).
      if (mode === viewMode) return;
      // Clear the deep-link bias and set the absolute mode together, and only
      // when the switch actually proceeds — so a guarded (dirty) switch the user
      // cancels leaves both the bias and the editor intact.
      const apply = () => {
        setDeepLinkBiasPath(null);
        setPreviewableViewMode(mode);
      };
      if (viewMode === "editor") {
        guardDirty(apply);
      } else {
        apply();
      }
    };
    // One toolbar slot: a "view mode" picker rather than three side-by-side
    // buttons (the toolbar is tight once nav/diff/comment actions are present).
    // The trigger shows the current surface's icon so the active mode reads at
    // a glance; the menu lets the user pick another.
    const modeOptions: ToolbarOption[] = [
      {
        key: "md-preview",
        label: "Preview",
        tooltip: "Rendered preview",
        icon: <EyeIcon className="size-4" />,
        active: viewMode === "preview",
        onSelect: () => switchTo("preview"),
      },
      {
        key: "md-edit",
        label: "Edit",
        tooltip: "Rich text editor",
        icon: <PencilLineIcon className="size-4" />,
        active: viewMode === "editor",
        onSelect: () => switchTo("editor"),
      },
      {
        key: "md-source",
        label: "Source",
        tooltip: "Raw Markdown source",
        icon: <CodeIcon className="size-4" />,
        active: viewMode === "source",
        onSelect: () => switchTo("source"),
      },
    ];
    const activeMode = modeOptions.find((o) => o.active) ?? modeOptions[0];
    toolbarActions.push({
      key: "md-view-mode",
      label: `View mode: ${activeMode.label}`,
      tooltip: "View mode",
      icon: activeMode.icon,
      options: modeOptions,
    });
  } else if ((lang === "html" || isNotebookPath(path)) && viewMode !== "diff") {
    // HTML and notebooks have no rich-text editor — a single toggle flips
    // preview ↔ source.
    toolbarActions.push({
      key: "preview",
      label: viewMode === "preview" ? "View source" : "View preview",
      icon:
        viewMode === "preview" ? <CodeIcon className="size-4" /> : <EyeIcon className="size-4" />,
      // Write the absolute target keyed off the RESOLVED viewMode, not the raw
      // stored value: a shared "editor" preference (carried over from a markdown
      // file) resolves to "preview" for HTML, so a functional updater keyed on
      // "editor" would no-op the first click. Keying on viewMode makes one click
      // always reach the other surface.
      onSelect: () => {
        setPreviewableViewMode(viewMode === "preview" ? "source" : "preview");
      },
    });
  }
  // HTML artifacts can be popped out into their own browser tab for full-window
  // viewing. The artifact still runs in the same sandboxed, opaque-origin iframe
  // as the in-app preview (isolated from the host app) — just full-screen.
  if (lang === "html" && fileQuery.data && viewMode !== "diff") {
    toolbarActions.push({
      key: "open-new-tab",
      label: "Open in new tab",
      icon: <SquareArrowOutUpRightIcon className="size-4" />,
      onSelect: openHtmlInNewTab,
    });
  }
  // PDFs render through PdfViewer, which has no text/selection surface to
  // anchor comments to, so hide the comments toggle for them.
  if (!isPdf) {
    toolbarActions.push({
      key: "comments",
      label: commentsOpen ? "Hide comments" : "Show comments",
      icon: <MessageSquareTextIcon className="size-4" />,
      active: commentsOpen,
      onSelect: () => {
        commentsInitializedRef.current = true;
        setCommentsOpen((prev) => !prev);
      },
    });
  }
  if (isDiffAvailable) {
    toolbarActions.push({
      key: "diff",
      label: viewMode === "diff" ? "Exit diff view" : "Show diff",
      icon: <FileDiffIcon className="size-4" />,
      active: viewMode === "diff",
      onSelect: () => guardDirty(() => setDiffActive((prev) => !prev)),
    });
  }
  if (viewMode === "diff" && splitToggleAvailable) {
    toolbarActions.push({
      key: "diff-layout",
      label: diffLayout === "unified" ? "Split view" : "Unified view",
      icon:
        diffLayout === "unified" ? (
          <Columns2Icon className="size-4" />
        ) : (
          <RowsIcon className="size-4" />
        ),
      onSelect: () => setDiffLayout((l) => (l === "unified" ? "split" : "unified")),
    });
  }
  // A single "⋯" menu folds the view controls that were previously separate
  // top-level icons: Find in file and Download (all views), plus the diff-only
  // toggles (wrap lines, whitespace changes). Grouping them frees toolbar width
  // — handy when the viewer runs in a narrow pane — and mirrors GitHub's
  // diff-settings menu. The toggles keep the menu open; actions close it as they
  // hand off.
  const settingsMenu: ToolbarOption[] = [
    {
      key: "search",
      label: "Find in file",
      icon: <SearchIcon className="size-4" />,
      active: false,
      onSelect: openSearch,
    },
  ];
  if (!isDeletedFile && fileQuery.data) {
    settingsMenu.push({
      key: "download",
      label: "Download file",
      tooltip: fileQuery.data.truncated
        ? "Download (file was truncated — content may be incomplete)"
        : "Download",
      icon: <DownloadIcon className="size-4" />,
      active: false,
      onSelect: downloadFile,
    });
  }
  if (viewMode === "diff") {
    settingsMenu.push({
      key: "wrap-lines",
      label: "Wrap lines",
      tooltip: "Soft-wrap long lines (no horizontal scroll)",
      icon: <WrapTextIcon className="size-4" />,
      active: wrapLines,
      keepOpen: true,
      onSelect: () => setWrapLines((prev) => !prev),
    });
    settingsMenu.push({
      key: "hide-whitespace",
      label: "Hide whitespace changes",
      icon: hideWhitespace ? <EyeIcon className="size-4" /> : <EyeOffIcon className="size-4" />,
      active: hideWhitespace,
      keepOpen: true,
      // The eye icon already flips open/closed to show state — no check needed.
      noActiveCheck: true,
      onSelect: () => setHideWhitespace((prev) => !prev),
    });
  }
  toolbarActions.push({
    key: "view-settings",
    label: "View settings",
    tooltip: "View settings",
    icon: <MoreHorizontalIcon className="size-4" />,
    menu: settingsMenu,
  });
  toolbarActions.push({
    key: "copy-link",
    label: "Copy link to file",
    tooltip: linkCopied ? "Copied!" : "Copy link",
    icon: linkCopied ? (
      <CheckIcon className="size-4 text-green-500" />
    ) : (
      <Link2Icon className="size-4" />
    ),
    onSelect: copyFileLink,
  });

  const showNavButtons = currentNavIdx !== -1 && navigableFiles.length > 1 && !!onNavigateTo;
  const {
    headerRef: toolbarHeaderRef,
    backRef: toolbarBackRef,
    navRef: toolbarNavRef,
    pathMeasureRef: toolbarPathMeasureRef,
    chipRef: toolbarChipRef,
    measureRef: toolbarMeasureRef,
    collapsed: toolbarCollapsed,
  } = useToolbarOverflow(
    // Re-measure when anything that contributes to the required width changes:
    // the action set (per view mode), the save chip's presence, the prev/next
    // nav cluster's presence, or the file path (its intrinsic width feeds the
    // path-aware title reserve). Each shifts the constants the effect reads.
    [
      toolbarActions.map((a) => a.key).join(","),
      `back:${!frameless}`,
      `chip:${saveStatus !== "idle"}`,
      `nav:${showNavButtons}`,
      `path:${path}`,
    ].join("|"),
  );

  // The expanded inline row — rendered both offscreen (for measurement) and,
  // when it fits, as the visible toolbar. `interactive` is false for the
  // measurement clone so it stays out of the tab order / a11y tree.
  const renderActionButtons = (interactive: boolean) =>
    toolbarActions.map((action) =>
      action.menu ? (
        // A settings menu: one trigger opening a list of independent items
        // (toggles + actions). Toggles carry `keepOpen` so the menu stays open
        // as the user flips them; actions close it on select.
        <DropdownMenu key={action.key}>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={action.label}
                    tabIndex={interactive ? undefined : -1}
                  >
                    {action.icon}
                  </Button>
                </DropdownMenuTrigger>
              </TooltipTrigger>
              <TooltipContent>{action.tooltip ?? action.label}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <DropdownMenuContent align="end" className="w-auto min-w-40">
            {action.menu.map((item) => (
              <DropdownMenuItem
                key={item.key}
                className="whitespace-nowrap"
                onSelect={
                  interactive
                    ? (e) => {
                        if (item.keepOpen) e.preventDefault();
                        item.onSelect();
                      }
                    : undefined
                }
              >
                {item.icon}
                {item.label}
                {item.active && !item.noActiveCheck && <CheckIcon className="ml-auto size-4" />}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : action.options ? (
        // A picker: one trigger opening a menu of mutually-exclusive choices.
        <DropdownMenu key={action.key}>
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger asChild>
                <DropdownMenuTrigger asChild>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={action.label}
                    tabIndex={interactive ? undefined : -1}
                  >
                    {action.icon}
                  </Button>
                </DropdownMenuTrigger>
              </TooltipTrigger>
              <TooltipContent>{action.tooltip ?? action.label}</TooltipContent>
            </Tooltip>
          </TooltipProvider>
          <DropdownMenuContent align="end" className="w-auto min-w-40">
            <DropdownMenuLabel>{action.tooltip ?? action.label}</DropdownMenuLabel>
            {action.options.map((option) => (
              <DropdownMenuItem
                key={option.key}
                className={cn("whitespace-nowrap", option.active && "bg-accent")}
                onSelect={interactive ? option.onSelect : undefined}
              >
                {option.icon}
                {option.label}
                {option.active && <CheckIcon className="ml-auto size-4" />}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      ) : (
        <TooltipProvider key={action.key}>
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                variant={action.active ? "default" : "ghost"}
                size="icon-sm"
                aria-label={action.label}
                tabIndex={interactive ? undefined : -1}
                onClick={interactive ? action.onSelect : undefined}
              >
                {action.icon}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{action.tooltip ?? action.label}</TooltipContent>
          </Tooltip>
        </TooltipProvider>
      ),
    );

  const innerContent = (
    <>
      <div
        ref={toolbarHeaderRef}
        className="flex min-w-0 shrink-0 items-center justify-between gap-2 border-b border-border px-4 py-3"
      >
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {/* Back button is the dismiss affordance for the mobile full-screen
              overlay only. On desktop the viewer is embedded in the tabbed
              Files rail, where tabs (and their x buttons) own open/close, so
              the back button would be a redundant "exit viewer" mode. */}
          {!frameless && (
            <div ref={toolbarBackRef} className="shrink-0">
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon-sm"
                      aria-label="Close file viewer"
                      onClick={() => guardDirty(onClose)}
                    >
                      <ArrowLeftIcon className="size-4" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Close</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            </div>
          )}
          {showNavButtons && (
            <div ref={toolbarNavRef} className="flex items-center gap-0.5 shrink-0">
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Previous file"
                disabled={!prevPath}
                onClick={() => prevPath && guardDirty(() => onNavigateTo(prevPath))}
              >
                <ChevronLeftIcon className="size-4" />
              </Button>
              <span className="text-[10px] text-muted-foreground tabular-nums">
                {currentNavIdx + 1}/{navigableFiles.length}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="Next file"
                disabled={!nextPath}
                onClick={() => nextPath && guardDirty(() => onNavigateTo(nextPath))}
              >
                <ChevronRightIcon className="size-4" />
              </Button>
            </div>
          )}
          {/* Always show the file path/name in the toolbar, in every view. */}
          <span className="min-w-0 truncate font-mono text-xs text-muted-foreground">{path}</span>
        </div>
        <div
          className="relative flex min-w-0 items-center justify-end gap-1"
          data-testid="FILESTOOLBAR"
        >
          {/* Auto-save status chip (replaces the editor's old Save button).
              Non-idle implies an editable Monaco buffer, so no extra gating.
              Kept outside the responsive switcher — always inline. */}
          {saveStatus !== "idle" && (
            <span
              ref={toolbarChipRef}
              aria-live="polite"
              title={
                saveStatus === "offline"
                  ? "Runner offline — your changes will save when it reconnects"
                  : undefined
              }
              className={cn(
                "mr-1 flex shrink-0 items-center gap-1 whitespace-nowrap text-[11px]",
                saveStatus === "error" ? "text-destructive" : "text-muted-foreground",
              )}
            >
              {saveStatus === "unsaved" && (
                <>
                  <span className="size-1.5 rounded-full bg-muted-foreground/70" />
                  Unsaved
                </>
              )}
              {saveStatus === "saving" && (
                <>
                  <Loader2Icon className="size-3 animate-spin" />
                  Saving…
                </>
              )}
              {saveStatus === "saved" && (
                <>
                  <CheckIcon className="size-3 text-green-500" />
                  Saved
                </>
              )}
              {saveStatus === "error" && (
                <>
                  <AlertTriangleIcon className="size-3" />
                  Save failed
                </>
              )}
              {saveStatus === "offline" && (
                <>
                  <CloudOffIcon className="size-3" />
                  Unsaved
                </>
              )}
            </span>
          )}
          {/* Responsive action switcher: inline icon buttons until they no
              longer fit (decided by useToolbarOverflow from the header width),
              then a single "⋯" menu with the same options. The offscreen clone
              below measures the full row's natural width. */}
          <div className="flex items-center justify-end gap-1">
            {toolbarCollapsed ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button type="button" variant="ghost" size="icon-sm" aria-label="More actions">
                    <MoreHorizontalIcon className="size-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-auto min-w-40">
                  {toolbarActions.map((action) =>
                    action.options || action.menu ? (
                      // Pickers and settings menus collapse to a nested submenu
                      // of their items. Toggle items (keepOpen) prevent the
                      // submenu from closing so several can be flipped in a row.
                      <DropdownMenuSub key={action.key}>
                        <DropdownMenuSubTrigger className="whitespace-nowrap">
                          {action.icon}
                          {action.tooltip ?? action.label}
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent>
                          {(action.options ?? action.menu ?? []).map((option) => (
                            <DropdownMenuItem
                              key={option.key}
                              // Settings-menu items lean on the check mark alone;
                              // the mutually-exclusive picker also highlights the
                              // active choice.
                              className={cn(
                                "whitespace-nowrap",
                                action.options && option.active && "bg-accent",
                              )}
                              onSelect={(e) => {
                                if (option.keepOpen) e.preventDefault();
                                option.onSelect();
                              }}
                            >
                              {option.icon}
                              {option.label}
                              {option.active && !option.noActiveCheck && (
                                <CheckIcon className="ml-auto size-4" />
                              )}
                            </DropdownMenuItem>
                          ))}
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                    ) : (
                      <DropdownMenuItem
                        key={action.key}
                        className="whitespace-nowrap"
                        onSelect={action.onSelect}
                      >
                        {action.icon}
                        {action.tooltip ?? action.label}
                      </DropdownMenuItem>
                    ),
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : (
              renderActionButtons(true)
            )}
          </div>
          {/* Offscreen measurement clones — rendered out of flow and out of the
              a11y tree purely so the overflow hook can read intrinsic widths
              that don't depend on the live (flex-coupled) layout:
                · the full expanded button row → its scrollWidth,
                · the file path at its natural, unconstrained width → its
                  offsetWidth (drives the path-aware title reserve). */}
          <div
            ref={toolbarMeasureRef}
            aria-hidden
            className="pointer-events-none absolute left-[-9999px] top-0 flex flex-nowrap items-center gap-1"
          >
            {renderActionButtons(false)}
          </div>
          <span
            ref={toolbarPathMeasureRef}
            aria-hidden
            className="pointer-events-none absolute left-[-9999px] top-0 font-mono text-xs whitespace-nowrap"
          >
            {path}
          </span>
        </div>
      </div>

      <div className="min-h-0 flex-1 flex flex-col md:flex-row overflow-hidden">
        <div ref={contentAreaRef} className="flex-1 overflow-y-auto min-w-0">
          {isDeletedFile && viewMode !== "diff" ? (
            <div className="flex flex-col items-center justify-center gap-2 p-8 text-sm text-muted-foreground">
              <Trash2Icon className="size-5 opacity-40" />
              <span>This file has been deleted.</span>
              {isDiffAvailable && (
                <span className="text-xs">
                  Click <FileDiffIcon className="inline size-3.5 align-text-bottom" /> to view its
                  previous content.
                </span>
              )}
            </div>
          ) : viewMode === "diff" ? (
            // A failed diff fetch (e.g. the diff endpoint returned a
            // git_status_failed 500) surfaces the server's reason instead of
            // hanging on "Loading diff…" forever — diffQuery.data stays
            // undefined on error, which would otherwise read as still-loading.
            diffQuery.isError ? (
              <div className="flex items-center justify-center p-8 text-destructive text-sm">
                Failed to load:{" "}
                {diffQuery.error instanceof Error
                  ? diffQuery.error.message
                  : String(diffQuery.error)}
              </div>
            ) : // Wait for the diff payload before mounting Monaco. useFileDiff uses
            // null to mean new (before=null) / deleted (after=null) file, so
            // collapsing the not-yet-loaded state into null would mount with the
            // wrong content and mis-set EOL (onMount runs once). Once data is
            // present, pass the real before/after through (legitimate nulls and all).
            !diffQuery.data ? (
              <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
                Loading diff…
              </div>
            ) : (
              <Suspense
                fallback={
                  <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
                    Loading diff…
                  </div>
                }
              >
                {/* key={path} remounts per file so onMount re-runs (EOL + comment
                  wiring re-applied) and `ready` resets while the new grammar loads. */}
                <MonacoDiffViewer
                  key={path}
                  before={diffQuery.data.before}
                  after={diffQuery.data.after}
                  path={path}
                  layout={diffLayout}
                  hideWhitespace={hideWhitespace}
                  wrapLines={wrapLines}
                  conversationId={conversationId}
                  comments={openComments}
                  activeSelection={activeSelection}
                  onSetActiveSelection={handleSetActiveSelection}
                  pendingBodyRef={pendingBodyRef}
                />
              </Suspense>
            )
          ) : (
            <CodeViewer
              conversationId={conversationId}
              path={path}
              fileQuery={fileQuery}
              onDirtyChange={setIsEditorDirty}
              onSaveStatusChange={setSaveStatus}
              comments={openComments}
              activeSelection={activeSelection}
              onSetActiveSelection={handleSetActiveSelection}
              pendingBodyRef={pendingBodyRef}
              panelOpen={open}
              searchOpen={searchOpen}
              setSearchOpen={setSearchOpen}
              searchInputRef={searchInputRef}
              viewMode={viewMode}
            />
          )}
        </div>
        {commentsOpen && (
          <CommentsPanel
            comments={openComments}
            addressedComments={addressedComments}
            activeSelection={activeSelection}
            pendingBodyRef={pendingBodyRef}
            onCopyCommentLink={copyCommentLink}
            onAddComment={(body) => {
              if (activeSelection == null) return;
              addComment.mutate(
                {
                  path,
                  start_index: activeSelection.start_index,
                  end_index: activeSelection.end_index,
                  body,
                  anchor_content: activeSelection.anchor_content,
                },
                { onSuccess: () => setActiveSelection(null) },
              );
            }}
            canAddress={canEdit && sender !== null}
            onAddressAll={() => {
              if (!sender) return;
              const ids = openComments.map((c) => c.id);
              sender.mutate({ comment_ids: ids });
              setActiveSelection(null);
            }}
            onClickComment={(comment) => {
              setActiveSelection({
                start_index: comment.start_index,
                end_index: comment.end_index,
                anchor_content: comment.anchor_content ?? "",
              });
              // Sync the selected comment into the URL so the address bar is
              // always shareable. AppShell clears this param when the viewer closes.
              setSearchParams(
                (prev) => {
                  const next = new URLSearchParams(prev);
                  next.set("comment", comment.id);
                  return next;
                },
                { replace: true },
              );
            }}
            onEditComment={(id, body) => updateComment.mutate({ commentId: id, body })}
            onDeleteComment={(id) => {
              deleteComment.mutate(id);
              const deleted = [...openComments, ...addressedComments].find((c) => c.id === id);
              if (
                deleted &&
                activeSelection?.start_index === deleted.start_index &&
                activeSelection?.end_index === deleted.end_index
              )
                setActiveSelection(null);
            }}
            addressPending={sender?.isPending ?? false}
            canEdit={canEdit}
          />
        )}
      </div>
      <Dialog
        open={pendingAction !== null}
        onOpenChange={(open) => {
          if (!open) setPendingAction(null);
        }}
      >
        <DialogContent showCloseButton={false}>
          <DialogHeader>
            <DialogTitle>Unsaved changes</DialogTitle>
            <DialogDescription>
              Your edits will be lost if you leave without saving.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingAction(null)}>
              Keep editing
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setIsEditorDirty(false);
                pendingAction?.();
                setPendingAction(null);
              }}
            >
              Discard changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );

  if (frameless) {
    return (
      <div
        data-testid="file-viewer"
        className="flex flex-col flex-1 min-h-0 overflow-hidden bg-card"
      >
        {innerContent}
      </div>
    );
  }

  return (
    <aside
      data-testid="file-viewer"
      style={{ width: panelWidth }}
      className={cn(
        "flex flex-col overflow-hidden bg-card transition-[translate,border-color,border-width] duration-150 ease-out",
        // Mobile (default): fixed full-screen overlay, slide via translate-x.
        "fixed inset-0 z-50 shadow-lg",
        open ? "translate-x-0" : "translate-x-full",
        // Desktop: static flex sibling; inline width from resize hook.
        "md:relative md:inset-auto md:z-auto md:shadow-none md:translate-x-0 md:shrink-0",
        open ? "md:border-border md:border-l" : "md:w-0 md:border-l-0",
      )}
      aria-hidden={!open}
      data-collapsed={!open || undefined}
      inert={!open}
    >
      {/* Resize handle — desktop only (mobile is full-screen overlay) */}
      {isDesktop && (
        <div
          {...handleProps}
          className="absolute inset-y-0 left-0 z-10 w-1 cursor-col-resize hover:bg-primary/30 active:bg-primary/50 transition-colors"
        />
      )}
      {open && innerContent}
    </aside>
  );
}

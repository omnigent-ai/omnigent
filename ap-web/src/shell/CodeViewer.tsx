// Syntax-highlighted file body with find-in-file, text-selection comments,
// and interleaved comment indicators.
//
// Comment UX:
//   The user selects any text in the code view. A floating "Add Comment"
//   button appears near the end of the selection. Clicking it calls
//   onSetActiveSelection with the absolute content offsets and the selected
//   text as anchor_content. The parent (FileViewer) opens CommentsPanel.
//
//   Existing comments highlight the lines they span. Clicking inside a
//   highlighted range navigates to that comment in CommentsPanel.

import { createPortal } from "react-dom";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  ChevronDownIcon,
  ChevronUpIcon,
  MessageSquarePlusIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import type { BundledLanguage, ThemedToken } from "shiki";
import { highlightCode } from "@/components/ai-elements/code-block";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { type Comment } from "@/hooks/useComments";
import { useFileContent } from "@/hooks/useFileContent";
import { useCanEdit } from "@/hooks/usePermissions";
import { cn } from "@/lib/utils";
import { MarkdownRichTextViewer } from "./MarkdownRichTextViewer";
import {
  type ActiveSelection,
  type SaveStatus,
  detectLang,
  getSelectionOffsets,
  indexToLine,
  isBinaryPath,
  lineOverlapsSelection,
  withBlankLinkTarget,
} from "./codeViewerHelpers";
import { renderLineTokens } from "./codeViewerRendering";
import { TruncatedBanner } from "./TruncatedBanner";
import { getEmbedRoot } from "@/lib/host";

// Monaco is heavy (~MBs + worker); load it only when a non-markdown file is
// actually viewed, so the initial bundle and markdown/preview paths don't pay
// for it.
const MonacoCodeEditor = lazy(() =>
  import("./MonacoCodeEditor").then((m) => ({ default: m.MonacoCodeEditor })),
);

// ---------------------------------------------------------------------------
// MarkdownPreview — read-only render of Markdown content via react-markdown + GFM
// ---------------------------------------------------------------------------

// Width of the line-number gutter — must match the `w-12` Tailwind class on the gutter div.
const GUTTER_WIDTH = 48;

function MarkdownPreview({ content }: { content: string }) {
  return (
    <div className="px-6 py-4 overflow-auto h-full prose dark:prose-invert prose-sm max-w-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CodeViewer — syntax-highlighted file body with interleaved comment threads
// ---------------------------------------------------------------------------

export interface CodeViewerProps {
  conversationId: string;
  path: string;
  fileQuery: ReturnType<typeof useFileContent>;
  comments: Comment[];
  /** Highlights the selection range in the code. */
  activeSelection: ActiveSelection | null;
  /**
   * Called when the user finishes a text selection or clicks a comment
   * indicator. Passes the absolute offsets and anchor text so the parent
   * can open the CommentsPanel.
   */
  onSetActiveSelection: (
    sel: { start_index: number; end_index: number; anchor_content: string } | null,
  ) => void;
  panelOpen: boolean;
  searchOpen: boolean;
  setSearchOpen: (open: boolean) => void;
  searchInputRef: RefObject<HTMLInputElement | null>;
  viewMode: "editor" | "preview" | "source" | "diff";
  onDirtyChange?: (isDirty: boolean) => void;
  /**
   * Forwarded to the Monaco editor only — it reports its auto-save lifecycle so
   * FileViewer can render a status chip. The markdown editor carries its own
   * save status in its toolbar, so it doesn't use this.
   */
  onSaveStatusChange?: (status: SaveStatus) => void;
  /** Forwarded to MarkdownRichTextViewer → MarkdownCommentPlugin. */
  pendingBodyRef?: RefObject<string>;
}

export function CodeViewer({
  conversationId,
  path,
  fileQuery,
  comments,
  activeSelection,
  onSetActiveSelection,
  panelOpen,
  searchOpen,
  setSearchOpen,
  searchInputRef,
  viewMode,
  onDirtyChange,
  onSaveStatusChange,
  pendingBodyRef,
}: CodeViewerProps) {
  const canEdit = useCanEdit(conversationId);

  const [tokenLines, setTokenLines] = useState<ThemedToken[][] | null>(null);

  // Find-in-file state — hooks must appear before early returns.
  const [searchQuery, setSearchQuery] = useState("");
  const [currentMatchIdx, setCurrentMatchIdx] = useState(0);
  const matchLineRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  // Floating "Add Comment" button state — viewport coordinates.
  const [selectionAnchor, setSelectionAnchor] = useState<{
    x: number;
    y: number;
    start_index: number;
    end_index: number;
    anchor_content: string;
  } | null>(null);

  const codeContainerRef = useRef<HTMLDivElement>(null);
  // Set to true by Cmd/Ctrl+A so the copy handler can write raw content
  // (preserving newlines and excluding gutter line numbers).
  const selectAllPendingRef = useRef(false);
  // Stable refs so the mouseup handler can access current values without
  // being recreated on every change (avoids stale closure bugs).
  const commentsRef = useRef(comments);
  useEffect(() => {
    commentsRef.current = comments;
  }, [comments]);
  const onSetActiveSelectionRef = useRef(onSetActiveSelection);
  useEffect(() => {
    onSetActiveSelectionRef.current = onSetActiveSelection;
  }, [onSetActiveSelection]);
  const canEditRef = useRef(canEdit);
  useEffect(() => {
    canEditRef.current = canEdit;
  }, [canEdit]);

  const content = fileQuery.data?.content ?? "";
  // Server returns only a prefix for very large files. Editing + saving a
  // truncated buffer would overwrite the rest of the real file, so editors
  // must drop to read-only when this is set.
  const truncated = fileQuery.data?.truncated ?? false;
  const lang = detectLang(path);
  // Non-markdown files render in Monaco (read-only or editable by permission);
  // markdown keeps TipTap (editor) / Shiki (source) and HTML keeps its preview.
  const showMonaco = lang !== "markdown" && viewMode !== "preview";
  // Only the Shiki DOM path needs the per-line split; skip it in Monaco mode.
  const rawLines = useMemo(() => (showMonaco ? [] : content.split("\n")), [content, showMonaco]);

  // Kick off Shiki highlighting whenever content or language changes.
  useEffect(() => {
    if (showMonaco) return; // Monaco does its own highlighting.
    if (viewMode === "editor" && lang === "markdown") return;
    let cancelled = false;
    setTokenLines(null);
    if (!content) return;
    const cached = highlightCode(content, lang as BundledLanguage, (result) => {
      if (!cancelled) setTokenLines(result.tokens);
    });
    if (cached) setTokenLines(cached.tokens);
    return () => {
      cancelled = true;
    };
  }, [content, lang, viewMode, showMonaco]);

  // Scroll to the line containing the active selection when it changes
  // (e.g. user clicked a comment in the panel).
  useEffect(() => {
    if (activeSelection == null) return;
    const lineNum = indexToLine(activeSelection.start_index, rawLines);
    matchLineRefs.current.get(lineNum - 1)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeSelection]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    setCurrentMatchIdx(0);
  }, [searchQuery]);
  useEffect(() => {
    if (!searchOpen) setSearchQuery("");
  }, [searchOpen]);

  // Scroll the current search match into view.
  useEffect(() => {
    if (!searchQuery.trim()) return;
    const matches = rawLines
      .map((line, i) => (line.toLowerCase().includes(searchQuery.toLowerCase()) ? i : -1))
      .filter((i) => i !== -1);
    if (matches.length === 0) return;
    const idx = matches[currentMatchIdx % matches.length];
    matchLineRefs.current.get(idx)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [searchQuery, currentMatchIdx]); // eslint-disable-line react-hooks/exhaustive-deps

  // Open search on Cmd+F / Ctrl+F.
  // Don't intercept in markdown editor mode — the custom search bar isn't
  // available there, so let the browser's native find handle it instead.
  const isMarkdownEditor = viewMode === "editor" && lang === "markdown";
  useEffect(() => {
    // Skip in Monaco mode too — Monaco owns Cmd+F (native find).
    if (!panelOpen || isMarkdownEditor || showMonaco) return;
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "f") {
        e.preventDefault();
        setSearchOpen(true);
        setTimeout(() => searchInputRef.current?.focus(), 0);
      } else if ((e.metaKey || e.ctrlKey) && e.key === "a") {
        const container = codeContainerRef.current;
        if (!container) return;
        // Don't intercept when an input or textarea has focus (let the browser handle it).
        const active = document.activeElement;
        if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) return;
        e.preventDefault();
        const selection = window.getSelection();
        if (!selection) return;
        const range = document.createRange();
        range.selectNodeContents(container);
        selection.removeAllRanges();
        selection.addRange(range);
        selectAllPendingRef.current = true;
      } else if (e.key === "Escape") {
        setSearchOpen(false);
        setSearchQuery("");
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [panelOpen, isMarkdownEditor, showMonaco, setSearchOpen, searchInputRef]); // eslint-disable-line react-hooks/exhaustive-deps

  // In Monaco mode the custom search bar isn't rendered; the editor opens its
  // native find when `searchOpen` is set (once it has mounted), then calls this
  // to reset the flag so the next Find click re-triggers it.
  const handleSearchHandled = useCallback(() => setSearchOpen(false), [setSearchOpen]);

  // Show "Add Comment" button after the user finishes a text selection
  // within the code container. A plain click (collapsed selection) clears
  // the active comment highlight unless the user clicked a comment gutter icon.
  useEffect(() => {
    const container = codeContainerRef.current;
    if (!container) return;
    const handleMouseUp = (e: MouseEvent) => {
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);

      if (sel.isCollapsed) {
        // Plain click — gutter onClick handles its own comments; skip here.
        if ((e.target as Element).closest("[data-gutter-comment]")) return;
        // Check if the caret landed inside a comment range.
        if (container.contains(range.commonAncestorContainer)) {
          const offsets = getSelectionOffsets(range, container, rawLines);
          if (offsets) {
            const clicked = commentsRef.current.find(
              (c) => c.start_index <= offsets.start_index && offsets.start_index < c.end_index,
            );
            if (clicked) {
              onSetActiveSelectionRef.current({
                start_index: clicked.start_index,
                end_index: clicked.end_index,
                anchor_content: clicked.anchor_content ?? "",
              });
              return;
            }
          }
        }
        onSetActiveSelectionRef.current(null);
        return;
      }

      // Non-collapsed selection — show the "Add Comment" button.
      if (!canEditRef.current) return;
      if (!container.contains(range.commonAncestorContainer)) return;
      const anchor_content = sel.toString();
      if (!anchor_content.trim()) return;
      const offsets = getSelectionOffsets(range, container, rawLines);
      if (!offsets) return;
      // Use the first client rect so multi-line selections anchor to the
      // start of the selection rather than the bounding box of all lines.
      const firstRect = range.getClientRects()[0] ?? range.getBoundingClientRect();
      // Left-align with the selection start, but never inside the gutter.
      const containerLeft = container.getBoundingClientRect().left;
      setSelectionAnchor({
        x: Math.max(firstRect.left, containerLeft + GUTTER_WIDTH),
        y: firstRect.top - 6,
        ...offsets,
        anchor_content,
      });
    };
    container.addEventListener("mouseup", handleMouseUp);
    return () => container.removeEventListener("mouseup", handleMouseUp);
  }, [rawLines]); // eslint-disable-line react-hooks/exhaustive-deps

  // Dismiss the floating button on any mousedown outside of it.
  useEffect(() => {
    const handleMouseDown = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-add-comment-btn]")) {
        setSelectionAnchor(null);
      }
    };
    document.addEventListener("mousedown", handleMouseDown);
    return () => document.removeEventListener("mousedown", handleMouseDown);
  }, []);

  // When Cmd/Ctrl+A selected the entire container, intercept the copy event
  // and write the raw file content so line numbers and flex-layout artifacts
  // are excluded and newlines are preserved exactly.
  useEffect(() => {
    const handleCopy = (e: ClipboardEvent) => {
      if (!selectAllPendingRef.current) return;
      selectAllPendingRef.current = false;
      e.preventDefault();
      e.clipboardData?.setData("text/plain", content);
    };
    // Clear the flag on any mousedown so a manual selection won't accidentally
    // trigger the raw-content override.
    const clearFlag = () => {
      selectAllPendingRef.current = false;
    };
    document.addEventListener("copy", handleCopy);
    document.addEventListener("mousedown", clearFlag);
    return () => {
      document.removeEventListener("copy", handleCopy);
      document.removeEventListener("mousedown", clearFlag);
    };
  }, [content]);

  if (fileQuery.isLoading) {
    return (
      <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
        Loading…
      </div>
    );
  }
  if (fileQuery.isError) {
    return (
      <div className="p-8 text-destructive text-sm">
        Error loading file:{" "}
        {fileQuery.error instanceof Error ? fileQuery.error.message : String(fileQuery.error)}
      </div>
    );
  }
  if (fileQuery.data?.encoding === "base64" || isBinaryPath(path)) {
    return (
      <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
        Preview not available for binary files.
      </div>
    );
  }

  if (viewMode === "editor" && lang === "markdown") {
    return (
      <MarkdownRichTextViewer
        content={content}
        conversationId={conversationId}
        path={path}
        isSettled={fileQuery.isSuccess}
        truncated={truncated}
        onDirtyChange={onDirtyChange}
        comments={comments}
        activeSelection={activeSelection}
        onSetActiveSelection={onSetActiveSelection}
        pendingBodyRef={pendingBodyRef}
      />
    );
  }

  if (viewMode === "preview" && (lang === "markdown" || lang === "html")) {
    const preview =
      lang === "markdown" ? (
        <MarkdownPreview content={content} />
      ) : (
        <iframe
          srcDoc={withBlankLinkTarget(content)}
          // Keep the preview script-less and origin-less (no allow-scripts /
          // allow-same-origin). allow-popups lets a `target="_blank"` link
          // actually open; allow-popups-to-escape-sandbox means the opened
          // tab is a normal page, not itself sandboxed.
          sandbox="allow-popups allow-popups-to-escape-sandbox"
          title="HTML preview"
          className="w-full h-full border-0"
        />
      );
    // A truncated preview renders incomplete content; warn the user (the editor
    // and source surfaces already do). No layout change when not truncated.
    if (!truncated) return preview;
    return (
      <div className="flex h-full flex-col">
        <TruncatedBanner />
        <div className="min-h-0 flex-1">{preview}</div>
      </div>
    );
  }

  if (showMonaco) {
    return (
      <Suspense
        fallback={
          <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
            Loading…
          </div>
        }
      >
        <MonacoCodeEditor
          content={content}
          conversationId={conversationId}
          path={path}
          isSettled={fileQuery.isSuccess}
          truncated={truncated}
          onDirtyChange={onDirtyChange}
          onSaveStatusChange={onSaveStatusChange}
          searchOpen={searchOpen}
          onSearchHandled={handleSearchHandled}
          comments={comments}
          activeSelection={activeSelection}
          onSetActiveSelection={onSetActiveSelection}
          pendingBodyRef={pendingBodyRef}
        />
      </Suspense>
    );
  }

  // Compute matches for the current search query.
  const matches = searchQuery.trim()
    ? rawLines
        .map((line, i) => (line.toLowerCase().includes(searchQuery.toLowerCase()) ? i : -1))
        .filter((i) => i !== -1)
    : [];
  const safeMatchIdx = matches.length > 0 ? currentMatchIdx % matches.length : 0;

  // Build a map: line number → first comment starting on that line.
  const commentByLine = new Map<number, Comment>();
  for (const c of comments) {
    const ln = indexToLine(c.start_index, rawLines);
    if (!commentByLine.has(ln)) commentByLine.set(ln, c);
  }

  // Precompute absolute start offset of each line for character-level highlighting.
  const lineStarts: number[] = [];
  {
    let off = 0;
    for (const l of rawLines) {
      lineStarts.push(off);
      off += l.length + 1;
    }
  }

  return (
    <>
      {/* Read-only source view (e.g. markdown source) can also be truncated. */}
      {truncated && <TruncatedBanner />}
      {/* Find-in-file search bar */}
      {searchOpen && (
        <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-card/90 px-3 py-1.5 backdrop-blur">
          <SearchIcon className="size-3.5 shrink-0 text-muted-foreground" />
          <input
            ref={searchInputRef}
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && matches.length > 0) {
                e.preventDefault();
                if (e.shiftKey) {
                  setCurrentMatchIdx((i) => (i - 1 + matches.length) % matches.length);
                } else {
                  setCurrentMatchIdx((i) => (i + 1) % matches.length);
                }
              }
            }}
            placeholder="Find…"
            className="min-w-0 flex-1 bg-transparent text-xs outline-none"
          />
          <span className="shrink-0 text-xs text-muted-foreground">
            {searchQuery.trim()
              ? matches.length > 0
                ? `${safeMatchIdx + 1} / ${matches.length}`
                : "No results"
              : ""}
          </span>
          <button
            type="button"
            aria-label="Previous match"
            className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
            disabled={matches.length === 0}
            onClick={() => setCurrentMatchIdx((i) => (i - 1 + matches.length) % matches.length)}
          >
            <ChevronUpIcon className="size-3.5" />
          </button>
          <button
            type="button"
            aria-label="Next match"
            className="rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-40"
            disabled={matches.length === 0}
            onClick={() => setCurrentMatchIdx((i) => (i + 1) % matches.length)}
          >
            <ChevronDownIcon className="size-3.5" />
          </button>
          <button
            type="button"
            aria-label="Close search"
            className="rounded p-0.5 text-muted-foreground hover:bg-muted"
            onClick={() => {
              setSearchOpen(false);
              setSearchQuery("");
            }}
          >
            <XIcon className="size-3.5" />
          </button>
        </div>
      )}

      {/* GitHub Light/Dark backgrounds match the shiki themes used by highlightCode */}
      <div ref={codeContainerRef} className="font-mono text-xs bg-white dark:bg-[#0d1117]">
        {rawLines.map((rawLine, idx) => {
          const lineNum = idx + 1;
          const isMatchLine =
            searchQuery.trim() !== "" &&
            rawLines[idx].toLowerCase().includes(searchQuery.toLowerCase());
          const isCurrentMatch = isMatchLine && matches[safeMatchIdx] === idx;
          const commentOnLine = commentByLine.get(lineNum);
          const isActiveRange =
            activeSelection != null &&
            lineOverlapsSelection(
              idx,
              rawLines,
              activeSelection.start_index,
              activeSelection.end_index,
            );
          const tokens = tokenLines?.[idx] ?? null;

          // Column offsets within this line for character-level highlight overlay.
          const lineAbsStart = lineStarts[idx] ?? 0;
          const selStartCol = isActiveRange
            ? Math.max(0, activeSelection!.start_index - lineAbsStart)
            : 0;
          const selEndCol = isActiveRange
            ? Math.min(rawLine.length, activeSelection!.end_index - lineAbsStart)
            : 0;

          // Per-comment overlays for this line (shown whenever comments exist).
          const commentOverlays = comments
            .filter((c) => lineOverlapsSelection(idx, rawLines, c.start_index, c.end_index))
            .map((c) => ({
              id: c.id,
              startCol: Math.max(0, c.start_index - lineAbsStart),
              endCol: Math.min(rawLine.length, c.end_index - lineAbsStart),
              isSelected:
                activeSelection?.start_index === c.start_index &&
                activeSelection?.end_index === c.end_index,
            }))
            .filter((o) => o.endCol > o.startCol);
          const hasAnyHighlight = commentOverlays.length > 0 || isActiveRange;

          return (
            <div
              key={lineNum}
              ref={(el) => {
                if (el) matchLineRefs.current.set(idx, el);
                else matchLineRefs.current.delete(idx);
              }}
              className={cn(isCurrentMatch && "bg-yellow-200/40 dark:bg-yellow-700/30")}
            >
              <div className="flex items-stretch">
                {/* Gutter — line number; MessageCircleIcon when a comment starts here */}
                <div
                  data-gutter-comment={commentOnLine ? true : undefined}
                  className={cn(
                    "relative w-12 shrink-0 select-none border-r border-border text-xs",
                    "flex items-center justify-end px-2 py-0.5 leading-5",
                    commentOnLine
                      ? "cursor-pointer text-yellow-500 dark:text-yellow-400 hover:bg-muted/60"
                      : "text-muted-foreground/50",
                    hasAnyHighlight && "bg-yellow-500/10 dark:bg-yellow-400/15",
                  )}
                  onClick={() => {
                    if (commentOnLine) {
                      onSetActiveSelection({
                        start_index: commentOnLine.start_index,
                        end_index: commentOnLine.end_index,
                        anchor_content: commentOnLine.anchor_content ?? "",
                      });
                    }
                  }}
                >
                  <span>{lineNum}</span>
                </div>

                {/* Line content — data-line attribute used for offset computation */}
                <div
                  data-line={lineNum}
                  className="relative flex-1 overflow-hidden whitespace-pre-wrap break-all pl-3 py-0.5 leading-5"
                >
                  {/* Character-level highlight overlays (monospace: 1ch per character).
                      All comments show a dim base color; the active selection/comment
                      is rendered on top in a stronger color.
                      Known limitation: `ch` equals the advance width of "0" in the
                      font, so tabs, wide Unicode, and proportional glyphs can cause
                      slight misalignment with the actual text. Acceptable for v1. */}
                  {commentOverlays.map((o) => (
                    <span
                      key={o.id}
                      aria-hidden
                      className={cn(
                        "absolute inset-y-0 pointer-events-none",
                        o.isSelected
                          ? "bg-yellow-400/25 dark:bg-yellow-400/25"
                          : "bg-yellow-200/40 dark:bg-yellow-400/20",
                      )}
                      style={{
                        left: `calc(0.75rem + ${o.startCol}ch)`,
                        width: `${o.endCol - o.startCol}ch`,
                      }}
                    />
                  ))}
                  {/* New selection (not yet saved as a comment) gets the active color. */}
                  {isActiveRange &&
                    selEndCol > selStartCol &&
                    !comments.some(
                      (c) =>
                        c.start_index === activeSelection!.start_index &&
                        c.end_index === activeSelection!.end_index,
                    ) && (
                      <span
                        aria-hidden
                        className="absolute inset-y-0 bg-yellow-400/25 dark:bg-yellow-400/25 pointer-events-none"
                        style={{
                          left: `calc(0.75rem + ${selStartCol}ch)`,
                          width: `${selEndCol - selStartCol}ch`,
                        }}
                      />
                    )}
                  {tokens !== null
                    ? renderLineTokens(tokens, isMatchLine ? searchQuery : "", isCurrentMatch)
                    : rawLine}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Floating "Add Comment" button — rendered into document.body so that
          CSS transforms on ancestor elements don't break fixed positioning. */}
      {selectionAnchor &&
        createPortal(
          <button
            data-add-comment-btn
            type="button"
            className="fixed z-50 flex items-center gap-1.5 rounded-md border border-border bg-popover backdrop-blur-xl backdrop-saturate-150 px-2.5 py-1 text-xs font-medium text-foreground shadow-md hover:bg-secondary transition-colors"
            style={{
              left: selectionAnchor.x,
              top: selectionAnchor.y,
              transform: "translateY(-100%)",
            }}
            onClick={() => {
              onSetActiveSelection({
                start_index: selectionAnchor.start_index,
                end_index: selectionAnchor.end_index,
                anchor_content: selectionAnchor.anchor_content,
              });
              setSelectionAnchor(null);
              window.getSelection()?.removeAllRanges();
            }}
          >
            <MessageSquarePlusIcon className="size-3.5" />
            Add comment
          </button>,
          getEmbedRoot() ?? document.body,
        )}
    </>
  );
}

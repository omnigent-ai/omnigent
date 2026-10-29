// TipTap-based rich text editor for markdown files.
//
// Comment UX:
//   The user selects any text in the editor.  A floating "Add Comment" button
//   appears above the selection (rendered by MarkdownCommentPlugin).  Clicking
//   it creates a transient "pending" ProseMirror Decoration (blue highlight)
//   so the range stays visible while the user types in the comment textarea,
//   then calls onSetActiveSelection with absolute char offsets into the raw
//   file.  Existing comments are highlighted as yellow spans via Decorations
//   that remap automatically through transactions.
//
// Key design choices:
//   • Decorations never touch the document → markdown serialisation is clean.
//   • Comment anchors use the validated Markdown source map, with a
//     text-offset fallback for documents the map cannot verify.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangleIcon, Check, Copy, MessageSquareOffIcon } from "lucide-react";
import { useEditor, EditorContent } from "@tiptap/react";
import type { Transaction } from "@tiptap/pm/state";
import type { Node } from "@tiptap/pm/model";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { useCanEdit } from "@/hooks/usePermissions";
import { useSessionActive } from "@/hooks/useWorkspaceChangedFiles";
import { followLinkWithPopupFallback } from "@/lib/openLinkInApp";
import { useFileViewer } from "./FileViewerContext";
import { isWorkspaceRelativeSrc, resolveWorkspacePath } from "./TipTapWorkspaceImage";
import { ToolbarPlugin } from "./MarkdownEditorToolbar";
import { TableHandles } from "./TableBubbleMenu";
import { TruncatedBanner } from "./TruncatedBanner";
import { type FinishServerCheck, useMarkdownEditorSync } from "./useMarkdownEditorSync";
import { useScrollRestore } from "./useScrollRestore";
import { useEditorAutoSave } from "./useEditorAutoSave";
import { MarkdownCommentPlugin } from "./MarkdownCommentPlugin";
import { MarkdownLinkBubble } from "./MarkdownLinkBubble";
import { MarkdownSearchBar } from "./MarkdownSearchBar";
import {
  createCommentDecorationExtension,
  type CommentDecorationState,
} from "./TipTapCommentExtension";
import {
  createSearchDecorationExtension,
  type SearchDecorationState,
} from "./TipTapSearchExtension";
import { createMarkdownDocumentExtensions } from "./markdownDocumentExtensions";
import { isUserEditUpdate } from "./codeBlockLanguageEdit";
import { applyExternalMarkdown } from "./TipTapEditorHelpers";
import {
  installMarkdownParserPatch,
  installMarkdownSerializerPatch,
} from "./tiptapMarkdownPatches";

// Minimal-escaping serialiser override (see tiptapMarkdownPatches.ts) —
// installed once at module load, before any editor instance is created.
installMarkdownSerializerPatch();

// Post-parse normalisation wrapping loose inline runs (a standalone image in
// document flow, `1. ![x](y)`) in a paragraph so block+ containers never get
// a bare inline child — the crash residual #2320 documented (see
// tiptapMarkdownPatches.ts).
installMarkdownParserPatch();

// TipTap drops a file's final newline, and the empty paragraph it appends after
// a closing heading, code block or footnote list saves as blank lines. End the
// file the way it was loaded: with one newline, or none.
function readMarkdown(editor: { getMarkdown: () => string }, endsWithNewline: boolean): string {
  const markdown = editor.getMarkdown().replace(/\n+$/, "");
  return endsWithNewline && markdown !== "" ? `${markdown}\n` : markdown;
}

// ---------------------------------------------------------------------------
// MarkdownRichTextViewer — outer shell manages the editor key for remounting
// ---------------------------------------------------------------------------

interface MarkdownRichTextViewerProps {
  content: string;
  conversationId: string;
  path: string;
  isSettled: boolean;
  /**
   * Server returned only a prefix of a large file. Editing is disabled
   * (read-only) so a save can't overwrite the unsent remainder.
   */
  truncated?: boolean;
  onDirtyChange?: (isDirty: boolean) => void;
  /** All saved comments for this file, used to restore highlights on mount. */
  comments: Comment[];
  /** The parent's current active selection. */
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /** Ref to the in-progress comment body; forwarded to MarkdownCommentPlugin. */
  pendingBodyRef?: React.RefObject<string>;
  /** True when the toolbar "Find in file" toggle wants the search bar open. */
  searchOpen?: boolean;
  /** Called when the search bar is closed (Escape / ✕) so the parent can reset the toggle. */
  onSearchHandled?: () => void;
  searchInputRef?: React.RefObject<HTMLInputElement | null>;
}

export function MarkdownRichTextViewer({
  content,
  conversationId,
  path,
  isSettled,
  truncated = false,
  onDirtyChange,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
  searchOpen,
  onSearchHandled,
  searchInputRef,
}: MarkdownRichTextViewerProps) {
  // A truncated buffer must never be editable, regardless of permission.
  const canEdit = useCanEdit(conversationId) && !truncated;

  // True while the owning session's agent turn is in flight — used to warn
  // before edits that would be overwritten by the agent's next write.
  const sessionActive = useSessionActive(conversationId);

  // Callback registered by the inner component once its TipTap editor is ready.
  // The sync hook calls this instead of remounting (setEditorKey) when an
  // external content update arrives on a clean editor — scroll and cursor are
  // preserved because the editor DOM is never torn down.
  const setContentRef = useRef<((content: string) => void) | null>(null);

  const {
    editorKey,
    isDirty,
    setDirty,
    hasExternalUpdate,
    discardAndApplyExternal,
    dismissExternalUpdate,
    markSaved,
    beginServerCheck,
  } = useMarkdownEditorSync({
    content,
    path,
    isSettled,
    onDirtyChange,
    setContentRef,
  });

  // This ref is shared across remounts — the ProseMirror plugin reads it.
  const commentStateRef = useRef<CommentDecorationState | null>(null);
  // Shared with the search plugin (find-in-file highlights).
  const searchStateRef = useRef<SearchDecorationState | null>(null);

  return (
    <MarkdownRichTextViewerInner
      // conversationId is part of the key: FileViewer is not keyed by session
      // at its mount sites, so a session switch with the same file open must
      // remount the editor — extensions close over conversationId/path and a
      // stale closure would fetch workspace images from the previous session.
      key={`${conversationId}:${editorKey}`}
      content={content}
      conversationId={conversationId}
      path={path}
      canEdit={canEdit}
      sessionActive={sessionActive}
      truncated={truncated}
      isDirty={isDirty}
      setDirty={setDirty}
      hasExternalUpdate={hasExternalUpdate}
      discardAndApplyExternal={discardAndApplyExternal}
      dismissExternalUpdate={dismissExternalUpdate}
      markSaved={markSaved}
      beginServerCheck={beginServerCheck}
      comments={comments}
      activeSelection={activeSelection}
      onSetActiveSelection={onSetActiveSelection}
      pendingBodyRef={pendingBodyRef}
      commentStateRef={commentStateRef}
      searchStateRef={searchStateRef}
      searchOpen={searchOpen ?? false}
      onSearchHandled={onSearchHandled}
      searchInputRef={searchInputRef}
      setContentRef={setContentRef}
    />
  );
}

// ---------------------------------------------------------------------------
// MarkdownRichTextViewerInner — TipTap editor instance
// ---------------------------------------------------------------------------

interface InnerProps {
  content: string;
  conversationId: string;
  path: string;
  canEdit: boolean;
  /** True while the owning session's agent turn is in flight. */
  sessionActive: boolean;
  truncated: boolean;
  isDirty: boolean;
  setDirty: (dirty: boolean) => void;
  hasExternalUpdate: boolean;
  discardAndApplyExternal: () => void;
  dismissExternalUpdate: () => void;
  markSaved: (content: string) => void;
  beginServerCheck: () => FinishServerCheck;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  pendingBodyRef?: React.RefObject<string>;
  commentStateRef: React.RefObject<CommentDecorationState | null>;
  searchStateRef: React.RefObject<SearchDecorationState | null>;
  searchOpen: boolean;
  onSearchHandled?: () => void;
  searchInputRef?: React.RefObject<HTMLInputElement | null>;
  setContentRef: React.RefObject<((content: string) => void) | null>;
}

function MarkdownRichTextViewerInner({
  content,
  conversationId,
  path,
  canEdit,
  sessionActive,
  truncated,
  isDirty,
  setDirty,
  hasExternalUpdate,
  discardAndApplyExternal,
  dismissExternalUpdate,
  markSaved,
  beginServerCheck,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
  commentStateRef,
  searchStateRef,
  searchOpen,
  onSearchHandled,
  searchInputRef,
  setContentRef,
}: InnerProps) {
  const openFile = useFileViewer();
  const [isCopied, setIsCopied] = useState(false);
  const copyTimeoutRef = useRef<number>(0);
  useEffect(
    () => () => {
      window.clearTimeout(copyTimeoutRef.current);
    },
    [],
  );
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const linkMouseDownRef = useRef<{ x: number; y: number } | null>(null);
  // Persist/restore the editor's scroll position across unmount/remount and
  // session switches. Content is present at mount (the parent gates on the
  // file query), so the restore is ready immediately.
  const handleScrollPersist = useScrollRestore(
    scrollContainerRef,
    conversationId && path ? `viewer-mdedit:${conversationId}:${path}` : null,
    true,
  );
  // Fall back to a local ref when the parent doesn't pass one (the toolbar
  // path always does; this keeps the bar usable in isolation/tests).
  const fallbackSearchInputRef = useRef<HTMLInputElement>(null);
  const localSearchInputRef = searchInputRef ?? fallbackSearchInputRef;

  const handleCopyContent = useCallback(() => {
    if (!navigator?.clipboard?.writeText) return;
    navigator.clipboard
      .writeText(content)
      .then(() => {
        setIsCopied(true);
        window.clearTimeout(copyTimeoutRef.current);
        copyTimeoutRef.current = window.setTimeout(() => setIsCopied(false), 2000);
      })
      .catch(() => {
        // ignore clipboard errors
      });
  }, [content]);

  // Baseline used to detect actual edits vs TipTap normalisation.
  const baselineRef = useRef<string | null>(null);
  const endsWithNewlineRef = useRef(content.endsWith("\n"));
  // Immutable snapshots share unchanged subtrees, so dirty checks need no
  // Markdown serialization and undo can still restore a clean document.
  const baselineDocRef = useRef<Node | null>(null);
  const savingDocRef = useRef<Node | null>(null);

  // Gates every save path: true only after a focused editor update (a real user
  // edit). TipTap's markdown round-trip isn't byte-stable, so getMarkdown() can
  // drift from the baseline with no edit (e.g. baseline captured against a
  // different editor instance across StrictMode's mount→unmount→remount); without
  // this gate an unmount/blur flush would persist that drift as a rewrite.
  const hasUserEditedRef = useRef(false);

  // Lets the auto-save / dirty accessors read the live editor without
  // re-creating callbacks. Assigned after useEditor returns.
  const editorRef = useRef<ReturnType<typeof useEditor>>(null);

  // Live dirty check (editor vs baseline, not React state) — used by auto-save
  // to decide whether there's anything to persist. Requires a real user edit so
  // a load-time normalisation drift can never be flushed to disk.
  const isEditorDirty = useCallback(() => {
    const ed = editorRef.current;
    return (
      hasUserEditedRef.current &&
      ed != null &&
      !ed.isDestroyed &&
      baselineDocRef.current !== null &&
      !ed.state.doc.eq(baselineDocRef.current)
    );
  }, []);

  const getContent = useCallback(() => {
    const ed = editorRef.current;
    if (!ed) return "";
    savingDocRef.current = ed.state.doc;
    const markdown = readMarkdown(ed, endsWithNewlineRef.current);
    // Document attributes can change without changing the saved Markdown.
    // The shared save hook skips the write (and markSaved) in that case.
    if (markdown === baselineRef.current) baselineDocRef.current = savingDocRef.current;
    return markdown;
  }, []);

  const markDocumentSaved = useCallback(
    (markdown: string) => {
      // Single-flight saves capture one snapshot; newer typing must not become
      // the baseline when an older write finishes.
      baselineDocRef.current = savingDocRef.current;
      markSaved(markdown);
    },
    [markSaved],
  );

  // All save orchestration — write mutation, pre-write conflict check, teardown
  // guard, and debounce/flush wiring — lives in the shared hook, identical to
  // the Monaco editor. This surface only supplies its own content access.
  const { autoSave, saveDisabled, writeFile } = useEditorAutoSave({
    conversationId,
    path,
    canEdit,
    isDirty,
    setDirty,
    hasExternalUpdate,
    markSaved: markDocumentSaved,
    beginServerCheck,
    dismissExternalUpdate,
    baselineRef,
    getContent,
    isEditorDirty,
  });

  // Extensions are created once per component mount.
  const extensions = useMemo(
    () => [
      ...createMarkdownDocumentExtensions(conversationId, path),
      createCommentDecorationExtension(commentStateRef),
      createSearchDecorationExtension(searchStateRef),
    ],
    // commentStateRef is stable and a path change remounts this component
    // (editorKey), so the closed-over conversationId/path can't go stale;
    // extensions must not change after mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const editor = useEditor({
    extensions,
    // contentType: 'markdown' tells @tiptap/markdown to parse the content
    // string as markdown rather than treating it as HTML/JSON.
    content,
    contentType: "markdown",
    editable: canEdit,
    onUpdate: ({ editor: ed, transaction }) => {
      // A transaction that changed no document is a no-op for our purposes (e.g.
      // setEditable's synthetic update). It can't have altered the markdown, so
      // re-baselining on it would only clobber an unsaved edit — skip entirely.
      if (transaction && !transaction.docChanged) return;
      // Only a genuine user edit should flag dirty. The first update, or any
      // update before the user focuses, is TipTap re-serialising the freshly
      // loaded doc — its markdown round-trip isn't byte-stable, so getMarkdown()
      // drifts from the on-disk bytes. Re-baseline instead of flagging dirty so
      // merely opening a file never autosaves a normalised rewrite. A code-block
      // language change is a user edit even though it arrives blurred (the
      // picker holds focus), so isUserEditUpdate also honours its flag.
      if (baselineDocRef.current === null || !isUserEditUpdate(ed.isFocused, transaction)) {
        baselineRef.current = readMarkdown(ed, endsWithNewlineRef.current);
        baselineDocRef.current = ed.state.doc;
        setDirty(false);
        return;
      }
      // A focused update is a real user edit — from here a save may write.
      hasUserEditedRef.current = true;
      setDirty(!ed.state.doc.eq(baselineDocRef.current));
    },
    onCreate: ({ editor: ed }) => {
      // Capture baseline after the initial content has been parsed.
      baselineRef.current = readMarkdown(ed, endsWithNewlineRef.current);
      baselineDocRef.current = ed.state.doc;
    },
  });
  editorRef.current = editor;

  // Auto-save wiring: debounce on edit, flush on blur. A programmatic
  // setContentRef (emitUpdate=false) emits no "update", so we never re-save
  // our own injected content.
  useEffect(() => {
    if (!editor) return;
    // Schedule only on user edits; a pre-focus normalisation update re-baselines
    // in onUpdate above and must not trigger a write. A blurred code-block
    // language change is a user edit (flagged on its transaction), so schedule
    // it too — otherwise the picker's change never persists.
    const onUpdate = (props?: { transaction?: Transaction }) => {
      // Mirror the onUpdate-config guard: a no-op transaction changed no markdown,
      // so there is nothing to save — never schedule a write for it.
      if (props?.transaction && !props.transaction.docChanged) return;
      if (isUserEditUpdate(editor.isFocused, props?.transaction)) autoSave.schedule();
    };
    const onBlur = () => autoSave.flush();
    editor.on("update", onUpdate);
    editor.on("blur", onBlur);
    return () => {
      editor.off("update", onUpdate);
      editor.off("blur", onBlur);
    };
  }, [editor, autoSave]);

  // In-place setter for the sync hook; skipping a remount keeps scroll and caret.
  // It emits no onUpdate, so re-baseline here, and restore scroll next frame
  // because selectionToDOM can scroll the caret into view.
  useEffect(() => {
    if (!editor) return;
    setContentRef.current = (newContent: string) => {
      if (editor.isDestroyed) return;
      const savedScroll = scrollContainerRef.current?.scrollTop ?? 0;
      applyExternalMarkdown(editor, newContent);
      endsWithNewlineRef.current = newContent.endsWith("\n");
      baselineRef.current = readMarkdown(editor, endsWithNewlineRef.current);
      baselineDocRef.current = editor.state.doc;
      // Programmatic replace establishes a fresh baseline — no user edits relative to it.
      hasUserEditedRef.current = false;
      setDirty(false);
      requestAnimationFrame(() => {
        if (scrollContainerRef.current) {
          scrollContainerRef.current.scrollTop = savedScroll;
        }
      });
    };
    return () => {
      setContentRef.current = null;
    };
  }, [editor, setContentRef, setDirty, path]);

  // Keep editor editable flag in sync with canEdit changes. Pass emitUpdate:
  // false so toggling editability doesn't fire a doc-less "update" (which the
  // onUpdate guard also ignores, but there's no reason to emit it at all).
  useEffect(() => {
    editor?.setEditable(canEdit, false);
  }, [editor, canEdit]);

  return (
    <div className="relative flex flex-col h-full">
      {truncated && <TruncatedBanner />}
      <MarkdownSearchBar
        editor={editor}
        searchStateRef={searchStateRef}
        open={searchOpen}
        onClose={() => onSearchHandled?.()}
        inputRef={localSearchInputRef}
      />
      {canEdit && (
        <ToolbarPlugin
          editor={editor}
          // Route manual saves (⌘S / pill) through the same single-flight +
          // trailing-save engine as auto-save, so a manual save during an
          // in-flight/debounced auto-save can't start an overlapping PUT.
          // flush() reads the live content itself, so the markdown arg is unused.
          onSave={() => autoSave.flush()}
          isSaving={writeFile.isPending}
          isDirty={isDirty}
          saveError={writeFile.isError}
          saveDisabled={saveDisabled}
          hasExternalUpdate={hasExternalUpdate}
        />
      )}
      <div
        ref={scrollContainerRef}
        onScroll={handleScrollPersist}
        className="relative flex-1 overflow-auto px-8 py-6"
        onMouseDown={(event) => {
          linkMouseDownRef.current = { x: event.clientX, y: event.clientY };
        }}
        // Follow links with one click in both modes. Alt-click places the
        // editing caret; Shift-click and dragging retain text selection.
        onClick={(event) => {
          const mouseDown = linkMouseDownRef.current;
          linkMouseDownRef.current = null;
          if (event.defaultPrevented || event.button !== 0) return;
          const anchor = (event.target as Element).closest("a[href]");
          if (!anchor) return;
          const href = anchor.getAttribute("href");
          // The editor may collapse a previous selection only after click.
          // Suppress navigation for a selection gesture, not that stale range.
          const selectingText =
            window.getSelection()?.isCollapsed === false &&
            (event.detail > 1 ||
              (mouseDown &&
                (Math.abs(event.clientX - mouseDown.x) > 4 ||
                  Math.abs(event.clientY - mouseDown.y) > 4)));
          if (
            !href ||
            /^[?#]/.test(href) ||
            selectingText ||
            (canEdit && !event.metaKey && !event.ctrlKey && (event.altKey || event.shiftKey))
          ) {
            event.preventDefault();
            return;
          }
          if (isWorkspaceRelativeSrc(href)) {
            event.preventDefault();
            const filePath = resolveWorkspacePath(path, href);
            if (filePath) openFile?.(filePath);
            return;
          }
          followLinkWithPopupFallback(event, href, conversationId);
          // Contenteditable anchors have no native navigation, including
          // modified clicks and the desktop shell's external-browser path.
          if (!event.defaultPrevented) {
            event.preventDefault();
            window.open(href, "_blank", "noopener,noreferrer");
          }
        }}
      >
        {!canEdit && (
          <button
            type="button"
            title="Copy"
            onClick={handleCopyContent}
            className="absolute top-3 right-3 z-10 flex items-center gap-1 rounded px-2 py-1 text-sm text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            {isCopied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            {isCopied ? "Copied!" : "Copy"}
          </button>
        )}
        <EditorContent
          editor={editor}
          className="outline-none max-w-none text-ui text-foreground [&_*::selection]:bg-blue-300/40 [&_*::selection]:text-foreground [&::selection]:bg-blue-300/40 [&::selection]:text-foreground tiptap-md-content"
        />
      </div>
      {canEdit && editor && (
        <TableHandles editor={editor} scrollContainerRef={scrollContainerRef} />
      )}
      {canEdit && editor && (
        <MarkdownLinkBubble editor={editor} path={path} scrollContainerRef={scrollContainerRef} />
      )}
      {canEdit && sessionActive && !isDirty && !hasExternalUpdate && (
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 border-t border-border bg-warning/10 px-4 py-1.5 text-sm text-foreground backdrop-blur-sm">
          <AlertTriangleIcon className="size-3.5 shrink-0 text-warning" />
          Omnigent is still working — edits you make now may be overwritten when it saves this file.
          Wait for it to finish, or copy your changes first.
        </div>
      )}
      {canEdit && isDirty && hasExternalUpdate && (
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-2 border-t border-border bg-warning/10 px-4 py-1.5 text-sm text-foreground backdrop-blur-sm">
          <AlertTriangleIcon className="size-3.5 shrink-0 text-warning" />
          <span className="flex-1">This file was modified externally while you were editing.</span>
          <button
            type="button"
            className="rounded px-2 py-0.5 font-medium hover:bg-muted transition-colors"
            onClick={dismissExternalUpdate}
          >
            Keep mine
          </button>
          <button
            type="button"
            className="rounded bg-primary px-2 py-0.5 font-medium text-primary-foreground hover:opacity-90 transition-opacity"
            onClick={discardAndApplyExternal}
          >
            Load latest
          </button>
        </div>
      )}
      {canEdit && isDirty && !hasExternalUpdate && saveDisabled && (
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 border-t border-border bg-warning/10 px-4 py-1.5 text-sm text-foreground backdrop-blur-sm">
          <MessageSquareOffIcon className="size-3.5 shrink-0 text-warning" />
          Runner offline — changes save and commenting resumes once it reconnects.
        </div>
      )}
      {canEdit && isDirty && !hasExternalUpdate && !saveDisabled && (
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 border-t border-border bg-muted/50 px-4 py-1.5 text-sm text-muted-foreground backdrop-blur-sm">
          <MessageSquareOffIcon className="size-3.5 shrink-0" />
          {writeFile.isPending ? "Saving…" : "Unsaved changes —"} commenting is available once
          saved.
        </div>
      )}
      <MarkdownCommentPlugin
        editor={editor}
        content={content}
        commentStateRef={commentStateRef}
        comments={comments}
        isDirty={isDirty}
        activeSelection={activeSelection}
        onSetActiveSelection={onSetActiveSelection}
        pendingBodyRef={pendingBodyRef}
        canEdit={canEdit}
      />
    </div>
  );
}

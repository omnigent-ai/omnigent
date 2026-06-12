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
//   • Comment anchor mapping uses doc.textBetween("\n") for plain-text offsets,
//     avoiding markdown-syntax drift.
//   • ProseMirror positions are stable integers; binary search maps text
//     offsets to PM positions without bespoke offset-inversion code.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangleIcon, Check, Copy, MessageSquareOffIcon } from "lucide-react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import {
  Table,
  TableRow,
  TableCell,
  TableHeader,
} from "@tiptap/extension-table";
import { Markdown } from "@tiptap/markdown";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection } from "./codeViewerHelpers";
import { useCanEdit } from "@/hooks/usePermissions";
import { ToolbarPlugin } from "./MarkdownEditorToolbar";
import { TableHandles } from "./TableBubbleMenu";
import { TruncatedBanner } from "./TruncatedBanner";
import { useMarkdownEditorSync } from "./useMarkdownEditorSync";
import { useEditorAutoSave } from "./useEditorAutoSave";
import { MarkdownCommentPlugin } from "./MarkdownCommentPlugin";
import {
  createCommentDecorationExtension,
  type CommentDecorationState,
} from "./TipTapCommentExtension";
import { createWorkspaceImageExtension, ImageAwareLink } from "./TipTapWorkspaceImage";
import { GitHubAlertBlockquote } from "./TipTapGitHubAlert";
import { HtmlPassthrough } from "./TipTapHtmlPassthrough";
import { installMarkdownSerializerPatch } from "./tiptapMarkdownPatches";

// Minimal-escaping serialiser override (see tiptapMarkdownPatches.ts) —
// installed once at module load, before any editor instance is created.
installMarkdownSerializerPatch();

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
}: MarkdownRichTextViewerProps) {
  // A truncated buffer must never be editable, regardless of permission.
  const canEdit = useCanEdit(conversationId) && !truncated;

  // Callback registered by the inner component once its TipTap editor is ready.
  // The sync hook calls this instead of remounting (setEditorKey) when an
  // external content update arrives on a clean editor — scroll and cursor are
  // preserved because the editor DOM is never torn down.
  const setContentRef = useRef<((content: string) => void) | null>(null);

  const { editorKey, isDirty, setDirty, hasExternalUpdate, discardAndApplyExternal, dismissExternalUpdate, markSaved, reconcileServerContent } = useMarkdownEditorSync({
    content,
    path,
    isSettled,
    onDirtyChange,
    setContentRef,
  });

  // This ref is shared across remounts — the ProseMirror plugin reads it.
  const commentStateRef = useRef<CommentDecorationState | null>(null);

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
      truncated={truncated}
      isDirty={isDirty}
      setDirty={setDirty}
      hasExternalUpdate={hasExternalUpdate}
      discardAndApplyExternal={discardAndApplyExternal}
      dismissExternalUpdate={dismissExternalUpdate}
      markSaved={markSaved}
      reconcileServerContent={reconcileServerContent}
      comments={comments}
      activeSelection={activeSelection}
      onSetActiveSelection={onSetActiveSelection}
      pendingBodyRef={pendingBodyRef}
      commentStateRef={commentStateRef}
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
  truncated: boolean;
  isDirty: boolean;
  setDirty: (dirty: boolean) => void;
  hasExternalUpdate: boolean;
  discardAndApplyExternal: () => void;
  dismissExternalUpdate: () => void;
  markSaved: (content: string) => void;
  reconcileServerContent: (serverContent: string) => boolean;
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  pendingBodyRef?: React.RefObject<string>;
  commentStateRef: React.RefObject<CommentDecorationState | null>;
  setContentRef: React.RefObject<((content: string) => void) | null>;
}

function MarkdownRichTextViewerInner({
  content,
  conversationId,
  path,
  canEdit,
  truncated,
  isDirty,
  setDirty,
  hasExternalUpdate,
  discardAndApplyExternal,
  dismissExternalUpdate,
  markSaved,
  reconcileServerContent,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
  commentStateRef,
  setContentRef,
}: InnerProps) {
  const [isCopied, setIsCopied] = useState(false);
  const copyTimeoutRef = useRef<number>(0);
  useEffect(() => () => { window.clearTimeout(copyTimeoutRef.current); }, []);
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  const handleCopyContent = useCallback(() => {
    if (!navigator?.clipboard?.writeText) return;
    navigator.clipboard
      .writeText(content)
      .then(() => {
        setIsCopied(true);
        window.clearTimeout(copyTimeoutRef.current);
        copyTimeoutRef.current = window.setTimeout(
          () => setIsCopied(false),
          2000,
        );
      })
      .catch(() => {
        // ignore clipboard errors
      });
  }, [content]);

  // Stable ref to the raw server content for comment offset mapping.
  const contentRef = useRef(content);
  useEffect(() => { contentRef.current = content; }, [content]);

  // Baseline used to detect actual edits vs TipTap normalisation.
  const baselineRef = useRef<string | null>(null);

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
      ed.getMarkdown() !== baselineRef.current
    );
  }, []);

  // All save orchestration — write mutation, mid-turn conflict check, teardown
  // guard, and debounce/flush wiring — lives in the shared hook, identical to
  // the Monaco editor. This surface only supplies its own content access.
  const { autoSave, saveDisabled, writeFile } = useEditorAutoSave({
    conversationId,
    path,
    canEdit,
    isDirty,
    setDirty,
    hasExternalUpdate,
    markSaved,
    reconcileServerContent,
    dismissExternalUpdate,
    baselineRef,
    getContent: () => editorRef.current?.getMarkdown() ?? "",
    isEditorDirty,
  });

  // Extensions are created once per component mount.
  const extensions = useMemo(
    () => [
      // link/blockquote: false — StarterKit bundles its own versions whose
      // markdown handlers would shadow the GitHub-flavored replacements
      // below (duplicate extension names: first wins).
      StarterKit.configure({ link: false, blockquote: false }),
      Table.configure({ resizable: true }),
      TableRow,
      TableCell,
      TableHeader,
      ImageAwareLink.configure({ openOnClick: false, autolink: false }),
      GitHubAlertBlockquote,
      HtmlPassthrough,
      Markdown,
      createWorkspaceImageExtension(conversationId, path),
      createCommentDecorationExtension(commentStateRef),
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
    onUpdate: ({ editor: ed }) => {
      const markdown = ed.getMarkdown();
      // Only a focused editor reflects a user edit. The first update, or any
      // update before the user focuses, is TipTap re-serialising the freshly
      // loaded doc — its markdown round-trip isn't byte-stable, so getMarkdown()
      // drifts from the on-disk bytes. Re-baseline instead of flagging dirty so
      // merely opening a file never autosaves a normalised rewrite.
      if (baselineRef.current === null || !ed.isFocused) {
        baselineRef.current = markdown;
        setDirty(false);
        return;
      }
      // A focused update is a real user edit — from here a save may write.
      hasUserEditedRef.current = true;
      setDirty(markdown !== baselineRef.current);
    },
    onCreate: ({ editor: ed }) => {
      // Capture baseline after the initial content has been parsed.
      baselineRef.current = ed.getMarkdown();
    },
  });
  editorRef.current = editor;

  // Auto-save wiring: debounce on edit, flush on blur. A programmatic
  // setContentRef (emitUpdate=false) emits no "update", so we never re-save
  // our own injected content.
  useEffect(() => {
    if (!editor) return;
    // Schedule only on focused (user) edits; a pre-focus normalisation update
    // re-baselines in onUpdate above and must not trigger a write.
    const onUpdate = () => { if (editor.isFocused) autoSave.schedule(); };
    const onBlur = () => autoSave.flush();
    editor.on("update", onUpdate);
    editor.on("blur", onBlur);
    return () => {
      editor.off("update", onUpdate);
      editor.off("blur", onBlur);
    };
  }, [editor, autoSave]);

  // Register an in-place content setter so the outer sync hook can update
  // the editor without triggering a full remount (preserves scroll/cursor).
  // emitUpdate=false suppresses onUpdate so we recapture baseline manually,
  // preventing the editor from being incorrectly flagged as dirty.
  //
  // Scroll is saved and restored via requestAnimationFrame: ProseMirror's
  // selectionToDOM re-establishes the cursor in the new DOM, which can cause
  // the browser to scroll the selection into view after the full-document
  // replace — undoing the user's scroll position.
  useEffect(() => {
    if (!editor) return;
    setContentRef.current = (newContent: string) => {
      if (editor.isDestroyed) return;
      const savedScroll = scrollContainerRef.current?.scrollTop ?? 0;
      editor.commands.setContent(newContent, { emitUpdate: false, contentType: "markdown" });
      baselineRef.current = editor.getMarkdown();
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

  // Keep editor editable flag in sync with canEdit changes.
  useEffect(() => {
    editor?.setEditable(canEdit);
  }, [editor, canEdit]);

  return (
    <div className="relative flex flex-col h-full">
      {truncated && <TruncatedBanner />}
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
        className="relative flex-1 overflow-auto px-8 py-6"
        onClick={
          !canEdit
            ? (e) => {
                const anchor = (e.target as Element).closest("a[href]");
                if (anchor) {
                  e.preventDefault();
                  window.open(anchor.getAttribute("href")!, "_blank", "noopener,noreferrer");
                }
              }
            : undefined
        }
      >
        {!canEdit && (
          <button
            type="button"
            title="Copy"
            onClick={handleCopyContent}
            className="absolute top-3 right-3 z-10 flex items-center gap-1 rounded px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground transition-colors"
          >
            {isCopied ? (
              <Check className="size-3.5" />
            ) : (
              <Copy className="size-3.5" />
            )}
            {isCopied ? "Copied!" : "Copy"}
          </button>
        )}
        <EditorContent
          editor={editor}
          className="outline-none max-w-none text-sm text-foreground [&_*::selection]:bg-blue-300/40 [&_*::selection]:text-foreground [&::selection]:bg-blue-300/40 [&::selection]:text-foreground tiptap-md-content"
        />
      </div>
      {canEdit && editor && (
        <TableHandles editor={editor} scrollContainerRef={scrollContainerRef} />
      )}
      {canEdit && isDirty && hasExternalUpdate && (
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-2 border-t border-border bg-warning/10 px-4 py-1.5 text-xs text-foreground backdrop-blur-sm">
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
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 border-t border-border bg-warning/10 px-4 py-1.5 text-xs text-foreground backdrop-blur-sm">
          <MessageSquareOffIcon className="size-3.5 shrink-0 text-warning" />
          Runner offline — changes save and commenting resumes once it reconnects.
        </div>
      )}
      {canEdit && isDirty && !hasExternalUpdate && !saveDisabled && (
        <div className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 border-t border-border bg-muted/50 px-4 py-1.5 text-xs text-muted-foreground backdrop-blur-sm">
          <MessageSquareOffIcon className="size-3.5 shrink-0" />
          {writeFile.isPending ? "Saving…" : "Unsaved changes —"} commenting is available once saved.
        </div>
      )}
      <MarkdownCommentPlugin
        editor={editor}
        contentRef={contentRef}
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

// Monaco-based viewer/editor for non-markdown files in the file viewer.
//
// One component serves both modes, switched by permission:
//   • read-only (no edit permission) → Monaco with readOnly:true; selection,
//     highlighting, and comment decorations still work.
//   • editable (edit permission)     → save via Cmd/Ctrl+S or the Save button.
//
// Highlighting comes from Shiki via @shikijs/monaco (github-light/dark), so
// colors match the read-only Shiki views and chat code blocks. The structure
// mirrors MarkdownRichTextViewer: an outer component owns the sync hook and
// remount key; the inner component owns the live editor instance.
//
// The comment layer (inline highlights, the floating "Add comment" button,
// click-to-navigate, reveal-on-select) lives in useMonacoCommentLayer, shared
// with the diff view. Adding a comment is gated on `canEdit && !isDirty`
// (offsets must match the saved server content).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Editor, type EditorProps, type OnChange, type OnMount } from "@monaco-editor/react";
import { useTheme } from "next-themes";
import { AlertTriangleIcon, MessageSquareOffIcon } from "lucide-react";
import { normalizeResolvedTheme } from "@/components/theme/themeMode";
import type { Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { detectLang, type ActiveSelection, type SaveStatus } from "./codeViewerHelpers";
import { TruncatedBanner } from "./TruncatedBanner";
// Reused as-is — the hook is editor-agnostic (drives any editor through
// setContentRef). Named for markdown only because that was its first caller.
import { useMarkdownEditorSync } from "./useMarkdownEditorSync";
import { useEditorAutoSave } from "./useEditorAutoSave";
import {
  ensureLanguage,
  ensureMonacoReady,
  monacoLanguageId,
  resolvedThemeToMonaco,
} from "./monacoSetup";
import { useMonacoCommentLayer, type CodeEditorInstance } from "./useMonacoCommentLayer";
import "./monacoCodeEditor.css";

type EditorOptions = EditorProps["options"];

// How long the transient "Saved" badge stays up before the status chip clears
// itself back to idle — long enough to register, short enough not to linger.
const SAVED_BADGE_MS = 2000;

interface CommentProps {
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /** In-progress comment body; clicking away won't clear an active draft. */
  pendingBodyRef?: React.RefObject<string>;
}

interface MonacoCodeEditorProps extends CommentProps {
  content: string;
  conversationId: string;
  path: string;
  /** True once the file query has settled (fileQuery.isSuccess). */
  isSettled: boolean;
  /**
   * Server returned only a prefix of a large file. Editing/saving is disabled
   * (read-only) to avoid overwriting the unsent remainder; viewing + commenting
   * on the visible prefix stay available.
   */
  truncated?: boolean;
  /** Reports unsaved-edit state up to FileViewer (navigation guard, etc.). */
  onDirtyChange?: (isDirty: boolean) => void;
  /** Reports the auto-save lifecycle up to FileViewer's toolbar status chip. */
  onSaveStatusChange?: (status: SaveStatus) => void;
  /**
   * True when the FileViewer "Find in file" button wants Monaco's native find
   * opened. The editor opens find once it has mounted (so a request made while
   * the lazy chunk is still loading isn't dropped), then calls onSearchHandled.
   */
  searchOpen?: boolean;
  /** Called after the editor has opened find, so the parent can reset the flag. */
  onSearchHandled?: () => void;
}

/**
 * Outer shell: owns the dirty/sync state and the remount key. Re-renders the
 * inner editor under a fresh `key` on path change (so each file starts clean),
 * and feeds same-file external updates in through `setContentRef`.
 *
 * @param props See {@link MonacoCodeEditorProps}.
 * @returns The Monaco code editor for non-markdown files.
 */
export function MonacoCodeEditor({
  content,
  conversationId,
  path,
  isSettled,
  truncated = false,
  onDirtyChange,
  onSaveStatusChange,
  searchOpen,
  onSearchHandled,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
}: MonacoCodeEditorProps) {
  // A truncated buffer must never be editable, regardless of permission.
  const canEdit = useCanEdit(conversationId) && !truncated;

  // Lets the sync hook push external content into the live editor without a
  // full remount, preserving scroll/cursor.
  const setContentRef = useRef<((content: string) => void) | null>(null);

  const {
    editorKey,
    isDirty,
    setDirty,
    hasExternalUpdate,
    discardAndApplyExternal,
    dismissExternalUpdate,
    markSaved,
    reconcileServerContent,
  } = useMarkdownEditorSync({ content, path, isSettled, onDirtyChange, setContentRef });

  return (
    <MonacoCodeEditorInner
      key={editorKey}
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
      setContentRef={setContentRef}
      onSaveStatusChange={onSaveStatusChange}
      searchOpen={searchOpen}
      onSearchHandled={onSearchHandled}
      comments={comments}
      activeSelection={activeSelection}
      onSetActiveSelection={onSetActiveSelection}
      pendingBodyRef={pendingBodyRef}
    />
  );
}

interface InnerProps extends CommentProps {
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
  setContentRef: React.RefObject<((content: string) => void) | null>;
  onSaveStatusChange?: (status: SaveStatus) => void;
  searchOpen?: boolean;
  onSearchHandled?: () => void;
}

/**
 * Inner editor instance. Holds the Monaco editor, tracks the saved baseline for
 * dirty detection, wires save + external-content sync + native find, and
 * delegates comment interactions to useMonacoCommentLayer.
 *
 * @param props See {@link InnerProps}.
 * @returns The editor surface plus its save bar / conflict banner.
 */
function MonacoCodeEditorInner({
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
  setContentRef,
  onSaveStatusChange,
  searchOpen,
  onSearchHandled,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
}: InnerProps) {
  const lang = detectLang(path);
  const { resolvedTheme } = useTheme();
  const monacoTheme = resolvedThemeToMonaco(normalizeResolvedTheme(resolvedTheme));

  // Gate rendering until Shiki has registered the github themes + this file's
  // grammar, so the editor never flashes Monaco's default 'vs' theme.
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState(false);
  useEffect(() => {
    let cancelled = false;
    // Re-gate on language change so the editor never renders against a
    // not-yet-registered grammar/theme — independent of any remount key.
    setReady(false);
    setLoadError(false);
    // Handle rejection explicitly: otherwise a failed Shiki/Monaco init is an
    // unhandled promise rejection and the view is stuck on "Loading…" forever.
    void Promise.all([ensureMonacoReady(), ensureLanguage(lang)]).then(
      () => {
        if (!cancelled) setReady(true);
      },
      () => {
        if (!cancelled) setLoadError(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [lang]);

  const editorInstanceRef = useRef<CodeEditorInstance | null>(null);
  // True once the editor instance exists; gates the comment-layer wiring.
  const [mounted, setMounted] = useState(false);
  // The last-saved content; edits are dirty when the buffer differs from it.
  const baselineRef = useRef<string | null>(content);
  // The live buffer content, tracked via onChange. Auto-save reads this rather
  // than editor.getValue() so a flush-on-unmount can run after Monaco has
  // already disposed the editor instance (the child <Editor> unmounts first).
  const latestContentRef = useRef(content);

  // Live dirty check (buffer vs saved baseline) — lets auto-save decide whether
  // there's anything to persist without re-reading the (possibly disposed-on-
  // unmount) editor. Reads latestContentRef, not editor.getValue(), so the
  // flush-on-unmount still sees a pending edit after Monaco has disposed the
  // instance (the child <Editor> unmounts first).
  const isEditorDirty = useCallback(() => latestContentRef.current !== baselineRef.current, []);

  // All save orchestration — write mutation, mid-turn conflict check, teardown
  // guard, and debounce/flush wiring — lives in the shared hook, identical to
  // the markdown editor. This surface only supplies its own content access.
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
    getContent: () => latestContentRef.current,
    isEditorDirty,
  });
  // Stable ref so the Monaco Cmd+S command + blur listener (registered once in
  // handleMount) always call the latest flush.
  const flushRef = useRef(autoSave.flush);
  flushRef.current = autoSave.flush;

  const handleMount: OnMount = useCallback(
    (editor, monaco) => {
      editorInstanceRef.current = editor;
      baselineRef.current = editor.getValue();
      latestContentRef.current = editor.getValue();
      // Keep Monaco's offsets aligned with the raw file's char offsets (which the
      // comment anchors use): enforce the file's existing EOL so a CRLF file isn't
      // silently counted as LF. Never converts — only re-asserts what's there.
      editor
        .getModel()
        ?.setEOL(
          content.includes("\r\n")
            ? monaco.editor.EndOfLineSequence.CRLF
            : monaco.editor.EndOfLineSequence.LF,
        );
      // Route ⌘S through the same single-flight + trailing-save engine as
      // auto-save, so a manual save during an in-flight/debounced auto-save can't
      // start an overlapping PUT.
      // oxlint-disable-next-line eslint(no-bitwise) -- Monaco keybindings are bit-OR'd flags.
      editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
        flushRef.current();
      });
      // Flush a pending debounce when focus leaves the editor (snappier than
      // waiting out the timer). Disposed with the editor on unmount.
      editor.onDidBlurEditorWidget(() => {
        flushRef.current();
      });
      // Push same-file external updates in place (preserves scroll/cursor).
      setContentRef.current = (newContent: string) => {
        const ed = editorInstanceRef.current;
        if (!ed) return;
        const viewState = ed.saveViewState();
        // Set the baseline before setValue so the onChange it triggers sees a
        // clean buffer and doesn't briefly flag the editor dirty.
        baselineRef.current = newContent;
        latestContentRef.current = newContent;
        ed.setValue(newContent);
        setDirty(false);
        if (viewState) ed.restoreViewState(viewState);
      };
      setMounted(true);
    },
    [setContentRef, setDirty, content],
  );

  useEffect(
    () => () => {
      setContentRef.current = null;
    },
    [setContentRef],
  );

  // Open Monaco's native find when the toolbar requests it. Gated on `mounted`
  // so a Find pressed while the lazy chunk was still loading isn't dropped —
  // when the editor mounts, `mounted` flips and this re-runs with searchOpen
  // still true. Calling getAction before the find contribution loads would
  // no-op, which is why we wait for the editor instance.
  useEffect(() => {
    if (!mounted || !searchOpen) return;
    editorInstanceRef.current?.getAction("actions.find")?.run();
    onSearchHandled?.();
  }, [mounted, searchOpen, onSearchHandled]);

  const handleChange: OnChange = useCallback(
    (value) => {
      const next = value ?? "";
      latestContentRef.current = next;
      const dirty = next !== baselineRef.current;
      setDirty(dirty);
      // Debounce an auto-save only on a real edit. A programmatic setValue (external
      // sync) re-baselines first, so this sees a clean buffer and won't schedule.
      if (dirty) autoSave.schedule();
    },
    [setDirty, autoSave],
  );

  // Surface the auto-save lifecycle to the FileViewer toolbar chip (this editor
  // no longer renders its own Save button). Ref'd so the effect doesn't re-run
  // when the parent passes a fresh callback identity.
  const onSaveStatusChangeRef = useRef(onSaveStatusChange);
  onSaveStatusChangeRef.current = onSaveStatusChange;
  const { isPending: writePending, isError: writeError, isSuccess: writeSuccess } = writeFile;
  useEffect(() => {
    // Order matters: a failed/in-flight write trumps the dirty flag; offline
    // dirty trumps a plain debounce; and isDirty is resolved before "saved" so
    // a stale isSuccess from the previous save doesn't mask fresh edits.
    // The error is gated on isDirty: once the user reverts to a clean buffer
    // there's nothing left to save, so a stale "Save failed" chip is cleared.
    let status: SaveStatus;
    if (writeError && isDirty) status = "error";
    else if (writePending) status = "saving";
    else if (saveDisabled && isDirty) status = "offline";
    else if (isDirty) status = "unsaved";
    else if (writeSuccess) status = "saved";
    else status = "idle";
    onSaveStatusChangeRef.current?.(status);
    // "Saved" is transient: clear it back to idle so the chip doesn't linger.
    if (status === "saved") {
      const t = window.setTimeout(() => onSaveStatusChangeRef.current?.("idle"), SAVED_BADGE_MS);
      return () => window.clearTimeout(t);
    }
  }, [writePending, writeError, writeSuccess, saveDisabled, isDirty]);

  // Clear the toolbar chip when this editor goes away (file switch / mode change
  // / panel close) so a stale "Saving…"/"Saved" doesn't outlive the editor.
  useEffect(
    () => () => {
      onSaveStatusChangeRef.current?.("idle");
    },
    [],
  );

  // Comments may be added only when editable and clean (offsets must match the
  // saved server content). Existing comments stay highlighted/navigable always.
  const commentButton = useMonacoCommentLayer({
    editorRef: editorInstanceRef,
    mounted,
    comments,
    activeSelection,
    onSetActiveSelection,
    canComment: canEdit && !isDirty,
    pendingBodyRef,
  });

  const options = useMemo<EditorOptions>(
    () => ({
      readOnly: !canEdit,
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      fontSize: 12,
      automaticLayout: true,
      renderLineHighlight: canEdit ? "line" : "none",
      // Read-only buffers still allow selection + copy; just hide the caret.
      cursorStyle: canEdit ? "line" : "underline-thin",
    }),
    [canEdit],
  );

  return (
    <div className="flex h-full flex-col">
      {truncated && <TruncatedBanner />}
      {canEdit &&
        isDirty &&
        (hasExternalUpdate ? (
          <div className="flex items-center gap-2 border-b border-border bg-warning/10 px-4 py-1.5 text-xs text-foreground shrink-0">
            <AlertTriangleIcon className="size-3.5 shrink-0 text-warning" />
            <span className="flex-1">
              This file was modified externally while you were editing.
            </span>
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
        ) : (
          <div className="flex items-center gap-1.5 border-b border-border bg-muted/50 px-4 py-1.5 text-xs text-muted-foreground shrink-0">
            <MessageSquareOffIcon className="size-3.5 shrink-0" />
            Save your changes to enable commenting on selections.
          </div>
        ))}
      <div className="relative min-h-0 flex-1">
        {loadError && (
          <div className="flex items-center justify-center p-8 text-destructive text-sm">
            Failed to load the editor.
          </div>
        )}
        {!loadError && !ready && (
          <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
            Loading…
          </div>
        )}
        {!loadError && ready && (
          <Editor
            height="100%"
            theme={monacoTheme}
            language={monacoLanguageId(lang)}
            defaultValue={content}
            options={options}
            onMount={handleMount}
            onChange={handleChange}
          />
        )}
      </div>
      {commentButton}
    </div>
  );
}

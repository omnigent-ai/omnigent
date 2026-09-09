// Monaco-based diff view for changed files (replaces the @pierre/diffs viewer).
//
// Shows before/after via Monaco's DiffEditor — inline (unified) or side-by-side
// (split) — with Shiki (github) highlighting so colors match the editor and the
// rest of the app. The modified side is read-only; comments work on it through
// the shared useMonacoCommentLayer (inline highlights + "Add comment" button +
// click-to-navigate), anchored by char offset into the current ("after") file.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DiffEditor, type DiffEditorProps, type DiffOnMount } from "@monaco-editor/react";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import {
  codeFontFamilyForEditor,
  readCodeFont,
  subscribeCodeFont,
} from "@/lib/codeFontPreferences";
import type { Comment } from "@/hooks/useComments";
import { useCanEdit } from "@/hooks/usePermissions";
import { detectLang, type ActiveSelection } from "./codeViewerHelpers";
import {
  ensureLanguage,
  ensureMonacoReady,
  monacoLanguageId,
  resolvedThemeToMonaco,
} from "./monacoSetup";
import { useMonacoCommentLayer, type CodeEditorInstance } from "./useMonacoCommentLayer";
import { attachEditorScrollRestore } from "./useScrollRestore";
import type { monaco } from "./monacoSetup";
import "./monacoCodeEditor.css";

// Monaco's find contribution isn't a public export, so we reach it by its
// registered id and describe only the slice we drive: whether the find widget
// is open, close it, and subscribe to open/close changes. Mirrors
// MonacoCodeEditor's wiring, applied to the diff's modified-side editor.
const FIND_CONTROLLER_ID = "editor.contrib.findController";
interface FindController extends monaco.editor.IEditorContribution {
  getState: () => {
    readonly isRevealed: boolean;
    onFindReplaceStateChange: (listener: (e: { isRevealed: boolean }) => void) => {
      dispose: () => void;
    };
  };
  closeFindWidget: () => void;
}

interface MonacoDiffViewerProps {
  /** File content before this session (null = new file). */
  before: string | null;
  /** Current file content (null = deleted file). */
  after: string | null;
  /** Workspace-relative file path, e.g. "src/foo.ts". */
  path: string;
  /** How hunks are rendered: side-by-side ("split") or inline ("unified"). */
  layout: "unified" | "split";
  /** Whether whitespace-only changes are hidden. */
  hideWhitespace: boolean;
  /** Whether long lines soft-wrap (no horizontal scroll). */
  wrapLines: boolean;
  conversationId: string;
  /** Saved comments — highlighted on the modified side. */
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /** In-progress comment body; clicking away won't clear an active draft. */
  pendingBodyRef?: React.RefObject<string>;
  /**
   * Toolbar / Cmd+F "Find in file" toggle. The diff mirrors Monaco's native
   * find widget (on the modified side) to it: true opens find, false closes it.
   * Cmd+F is driven through this flag rather than Monaco's own keybinding so it
   * fires even when the editor lacks DOM focus — the case that breaks in the
   * managed (same-root embed) host.
   */
  searchOpen?: boolean;
  /**
   * Called when the find widget is closed from within Monaco (Escape or the
   * widget's ✕) so the owning toggle resets and the next Cmd+F re-opens it.
   */
  onSearchHandled?: () => void;
}

/**
 * Render a file's before/after diff in Monaco, with the comment layer on the
 * modified side. Comments are gated on edit permission; the diff itself is
 * always read-only.
 *
 * @param props See {@link MonacoDiffViewerProps}.
 * @returns The diff editor surface plus the floating "Add comment" button.
 */
export function MonacoDiffViewer({
  before,
  after,
  path,
  layout,
  hideWhitespace,
  wrapLines,
  conversationId,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
  searchOpen,
  onSearchHandled,
}: MonacoDiffViewerProps) {
  const canEdit = useCanEdit(conversationId);
  const lang = detectLang(path);
  const monacoTheme = resolvedThemeToMonaco(useResolvedThemeMode());

  // Gate rendering until Shiki has registered the github themes + this file's
  // grammar (so the diff never flashes Monaco's default 'vs' theme); surface an
  // error rather than an unhandled rejection + permanent spinner on failure.
  const [ready, setReady] = useState(false);
  const [loadError, setLoadError] = useState(false);
  useEffect(() => {
    let cancelled = false;
    // Re-gate on language change so we never render the editor against a
    // not-yet-registered grammar/theme — independent of any remount key.
    setReady(false);
    setLoadError(false);
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

  // The modified-side code editor, obtained from the diff editor on mount.
  const modifiedEditorRef = useRef<CodeEditorInstance | null>(null);
  // The diff editor itself — its updateOptions propagates the code font to both
  // panes (per-pane updateOptions would only re-font one side).
  const diffEditorRef = useRef<Parameters<DiffOnMount>[0] | null>(null);
  // The two text models, captured on mount so we can dispose them ourselves on
  // unmount (see the teardown effect and keepCurrent* on <DiffEditor>).
  const originalModelRef = useRef<ReturnType<CodeEditorInstance["getModel"]>>(null);
  const modifiedModelRef = useRef<ReturnType<CodeEditorInstance["getModel"]>>(null);
  const [mounted, setMounted] = useState(false);

  // The diff scrolls inside Monaco, so its offset is cached per conversation +
  // file rather than via the DOM scroll-restore hook. Kept in its own namespace
  // so a file's diff and its editor view don't share one offset.
  const scrollKeyRef = useRef("");
  scrollKeyRef.current = `viewer-diff:${conversationId}:${path}`;

  const handleMount: DiffOnMount = useCallback(
    (diffEditor, monaco) => {
      diffEditorRef.current = diffEditor;
      const modified = diffEditor.getModifiedEditor();
      modifiedEditorRef.current = modified;
      // Capture both models so the teardown effect can dispose them itself.
      originalModelRef.current = diffEditor.getOriginalEditor()?.getModel() ?? null;
      modifiedModelRef.current = modified.getModel();
      // Align the modified model's offsets with the raw "after" char offsets that
      // comment anchors use (CRLF files would otherwise be counted as LF).
      modified
        .getModel()
        ?.setEOL(
          (after ?? "").includes("\r\n")
            ? monaco.editor.EndOfLineSequence.CRLF
            : monaco.editor.EndOfLineSequence.LF,
        );
      // Restore the reader's place in the diff and cache further scrolling under
      // the diff's own key.
      attachEditorScrollRestore(
        modified,
        () => scrollKeyRef.current,
        () => modifiedEditorRef.current === modified,
      );
      setMounted(true);
    },
    [after],
  );

  useEffect(
    () => () => {
      // @monaco-editor/react's DiffEditor disposes the text models before the
      // diff widget on unmount, which the bundled Monaco rejects with "TextModel
      // got disposed before DiffEditorWidget model got reset". We take model
      // disposal over via keepCurrent* on <DiffEditor>: detach the widget's
      // model first, then dispose the two models here. Guarded because our
      // cleanup and the library's own run in an unspecified order.
      try {
        diffEditorRef.current?.setModel(null);
      } catch {
        // Widget already disposed by the library — nothing to reset.
      }
      try {
        originalModelRef.current?.dispose();
      } catch {
        // Already disposed.
      }
      try {
        modifiedModelRef.current?.dispose();
      } catch {
        // Already disposed.
      }
      modifiedEditorRef.current = null;
      diffEditorRef.current = null;
      originalModelRef.current = null;
      modifiedModelRef.current = null;
    },
    [],
  );

  // Mirror the "Find in file" toggle to Monaco's native find widget on the
  // modified side. Gated on `mounted` so a Cmd+F pressed while the lazy chunk
  // was still loading isn't dropped. `searchOpen` true opens find; false closes
  // it. The controller drives the close directly so re-toggling is a real close,
  // not a second open.
  useEffect(() => {
    if (!mounted) return;
    const editor = modifiedEditorRef.current;
    if (!editor) return;
    if (searchOpen) {
      editor.getAction("actions.find")?.run();
    } else {
      const controller = editor.getContribution<FindController>(FIND_CONTROLLER_ID);
      if (controller?.getState().isRevealed) controller.closeFindWidget();
    }
  }, [mounted, searchOpen]);

  // Reflect a find close initiated inside Monaco (Escape or the widget's ✕) back
  // to the owning toggle, so its state matches the visible widget and the next
  // Cmd+F re-opens instead of no-opping.
  useEffect(() => {
    if (!mounted) return;
    const controller =
      modifiedEditorRef.current?.getContribution<FindController>(FIND_CONTROLLER_ID);
    if (!controller) return;
    const sub = controller.getState().onFindReplaceStateChange((e) => {
      if (e.isRevealed && !controller.getState().isRevealed) onSearchHandled?.();
    });
    return () => sub.dispose();
  }, [mounted, onSearchHandled]);

  // Apply live code-font changes to both diff panes. Monaco is a fixed-pixel
  // widget with no CSS-variable path like the chrome font, so the new
  // options must be pushed imperatively; the options memo seeds the initial
  // value at creation.
  useEffect(() => {
    return subscribeCodeFont((font) => {
      diffEditorRef.current?.updateOptions({
        fontSize: font.sizePx,
        fontFamily: codeFontFamilyForEditor(font.family),
        fontWeight: String(font.weight),
      });
    });
  }, []);

  // Comments anchor into the current ("after") content == the saved file, so
  // they're always offset-valid here; gate only on edit permission.
  const commentButton = useMonacoCommentLayer({
    editorRef: modifiedEditorRef,
    mounted,
    comments,
    activeSelection,
    onSetActiveSelection,
    canComment: canEdit,
    pendingBodyRef,
    path,
  });

  const options = useMemo<DiffEditorProps["options"]>(() => {
    const font = readCodeFont();
    return {
      readOnly: true, // modified side: view + select + comment, no editing
      originalEditable: false,
      renderSideBySide: layout === "split",
      // Below `renderSideBySideInlineBreakpoint` (900px) Monaco collapses
      // side-by-side into inline — a legitimate constraint for a usable diff.
      // FileViewer only surfaces the split/unified toggle once the diff area is
      // wide enough for split (see SPLIT_DIFF_MIN_WIDTH), so we leave Monaco's
      // responsive default in place rather than forcing split at any width.
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      // Code-font preference (Settings → Appearance), read at creation; live
      // changes arrive via updateOptions in the effect above. An unset family
      // resolves to the shared mono stack, so the diff matches the terminal
      // rather than falling back to Monaco's own platform default.
      fontSize: font.sizePx,
      fontFamily: codeFontFamilyForEditor(font.family),
      fontWeight: String(font.weight),
      automaticLayout: true,
      renderOverviewRuler: false,
      ignoreTrimWhitespace: hideWhitespace,
      // Soft-wrap long lines in both panes so a narrow diff pane (2–3 side by
      // side) reads top-to-bottom without horizontal scrolling.
      diffWordWrap: wrapLines ? "on" : "off",
      // Collapse long unchanged runs into expandable bands (like the old pierre
      // diff / GitHub) so only changed hunks + a few context lines are shown.
      hideUnchangedRegions: { enabled: true, contextLineCount: 3 },
    };
  }, [layout, hideWhitespace, wrapLines]);

  return (
    <div className="flex h-full flex-col">
      <div className="relative min-h-0 flex-1">
        {loadError && (
          <div className="flex items-center justify-center p-8 text-destructive text-ui">
            Failed to load the diff.
          </div>
        )}
        {!loadError && !ready && (
          <div className="flex items-center justify-center p-8 text-muted-foreground text-ui">
            Loading diff…
          </div>
        )}
        {!loadError && ready && (
          <DiffEditor
            height="100%"
            theme={monacoTheme}
            language={monacoLanguageId(lang)}
            original={before ?? ""}
            modified={after ?? ""}
            options={options}
            onMount={handleMount}
            // We dispose the models ourselves on unmount, in the correct order
            // (see the teardown effect). Without this the library disposes them
            // before the diff widget and Monaco throws.
            keepCurrentOriginalModel
            keepCurrentModifiedModel
          />
        )}
      </div>
      {commentButton}
    </div>
  );
}

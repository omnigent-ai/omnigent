// Monaco-based diff view for changed files (replaces the @pierre/diffs viewer).
//
// Shows before/after via Monaco's DiffEditor — inline (unified) or side-by-side
// (split) — with Shiki (github) highlighting so colors match the editor and the
// rest of the app. The modified side is read-only; comments work on it through
// the shared useMonacoCommentLayer (inline highlights + "Add comment" button +
// click-to-navigate), anchored by char offset into the current ("after") file.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DiffEditor, type DiffEditorProps, type DiffOnMount } from "@monaco-editor/react";
import { useTheme } from "next-themes";
import { normalizeResolvedTheme } from "@/components/theme/themeMode";
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
import "./monacoCodeEditor.css";

interface MonacoDiffViewerProps {
  /** File content before this session (null = new file). */
  before: string | null;
  /** Current file content (null = deleted file). */
  after: string | null;
  /** Workspace-relative file path, e.g. "src/foo.ts". */
  path: string;
  /** How hunks are rendered: side-by-side ("split") or inline ("unified"). */
  layout: "unified" | "split";
  conversationId: string;
  /** Saved comments — highlighted on the modified side. */
  comments: Comment[];
  activeSelection: ActiveSelection | null;
  onSetActiveSelection: (sel: ActiveSelection | null) => void;
  /** In-progress comment body; clicking away won't clear an active draft. */
  pendingBodyRef?: React.RefObject<string>;
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
  conversationId,
  comments,
  activeSelection,
  onSetActiveSelection,
  pendingBodyRef,
}: MonacoDiffViewerProps) {
  const canEdit = useCanEdit(conversationId);
  const lang = detectLang(path);
  const { resolvedTheme } = useTheme();
  const monacoTheme = resolvedThemeToMonaco(normalizeResolvedTheme(resolvedTheme));

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
  const [mounted, setMounted] = useState(false);

  const handleMount: DiffOnMount = useCallback(
    (diffEditor, monaco) => {
      const modified = diffEditor.getModifiedEditor();
      modifiedEditorRef.current = modified;
      // Align the modified model's offsets with the raw "after" char offsets that
      // comment anchors use (CRLF files would otherwise be counted as LF).
      modified
        .getModel()
        ?.setEOL(
          (after ?? "").includes("\r\n")
            ? monaco.editor.EndOfLineSequence.CRLF
            : monaco.editor.EndOfLineSequence.LF,
        );
      setMounted(true);
    },
    [after],
  );

  useEffect(
    () => () => {
      modifiedEditorRef.current = null;
    },
    [],
  );

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
  });

  const options = useMemo<DiffEditorProps["options"]>(
    () => ({
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
      fontSize: 12,
      automaticLayout: true,
      renderOverviewRuler: false,
      // Collapse long unchanged runs into expandable bands (like the old pierre
      // diff / GitHub) so only changed hunks + a few context lines are shown.
      hideUnchangedRegions: { enabled: true, contextLineCount: 3 },
    }),
    [layout],
  );

  return (
    <div className="flex h-full flex-col">
      <div className="relative min-h-0 flex-1">
        {loadError && (
          <div className="flex items-center justify-center p-8 text-destructive text-sm">
            Failed to load the diff.
          </div>
        )}
        {!loadError && !ready && (
          <div className="flex items-center justify-center p-8 text-muted-foreground text-sm">
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
          />
        )}
      </div>
      {commentButton}
    </div>
  );
}

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
import { cn } from "@/lib/utils";
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
   * Size the editor to its content instead of filling its container, and let
   * the page scroll rather than scrolling inside Monaco. For stacking many
   * diffs in one scroll view (the GitHub tab). Also disables the comment
   * affordance and per-file scroll restore, which only make sense for a single
   * full-height diff.
   */
  autoHeight?: boolean;
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
  autoHeight = false,
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
  const [mounted, setMounted] = useState(false);
  // Monaco paints the whole file first, then collapses unchanged regions once
  // the diff is computed — a visible expand→collapse flicker. Keep the editor
  // hidden until that first collapse settles, then fade it in.
  const [diffSettled, setDiffSettled] = useState(false);
  const updateDiffSubRef = useRef<{ dispose: () => void } | null>(null);
  const revealRafRef = useRef<number | null>(null);
  // autoHeight: the editor is sized to its content (see the effect in
  // handleMount). Null until the first measurement.
  const [contentHeight, setContentHeight] = useState<number | null>(null);
  const contentSizeSubsRef = useRef<{ dispose: () => void }[]>([]);
  // Placeholder height before the real content height is measured, so a
  // not-yet-mounted / still-computing section reserves roughly its space.
  const AUTO_HEIGHT_PLACEHOLDER = 160;

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
      // the diff's own key — only in fill mode; an auto-height diff has no inner
      // scroll to restore (the page scrolls).
      if (!autoHeight) {
        attachEditorScrollRestore(
          modified,
          () => scrollKeyRef.current,
          () => modifiedEditorRef.current === modified,
        );
      } else {
        // Size the container to the rendered content (max of both panes) and
        // keep it in sync as regions collapse/expand.
        const original = diffEditor.getOriginalEditor();
        const syncHeight = () => {
          const h = Math.max(
            original.getContentHeight?.() ?? 0,
            modified.getContentHeight?.() ?? 0,
          );
          if (h > 0) setContentHeight(h);
        };
        contentSizeSubsRef.current = [
          modified.onDidContentSizeChange?.(syncHeight),
          original.onDidContentSizeChange?.(syncHeight),
        ].filter((d): d is { dispose: () => void } => d != null);
        syncHeight();
      }
      // Reveal once the diff is computed (and its region collapse has painted),
      // deferred a frame so the collapsed layout lands before we fade in. The
      // API is absent in the jsdom test mock, so guard it; a fallback timeout
      // below reveals regardless.
      const withDiffUpdate = diffEditor as unknown as {
        onDidUpdateDiff?: (cb: () => void) => { dispose: () => void };
      };
      if (typeof withDiffUpdate.onDidUpdateDiff === "function") {
        updateDiffSubRef.current = withDiffUpdate.onDidUpdateDiff(() => {
          if (revealRafRef.current !== null) cancelAnimationFrame(revealRafRef.current);
          revealRafRef.current = requestAnimationFrame(() => setDiffSettled(true));
        });
      }
      setMounted(true);
    },
    [after, autoHeight],
  );

  useEffect(
    () => () => {
      modifiedEditorRef.current = null;
      diffEditorRef.current = null;
      updateDiffSubRef.current?.dispose();
      updateDiffSubRef.current = null;
      contentSizeSubsRef.current.forEach((d) => d.dispose());
      contentSizeSubsRef.current = [];
      if (revealRafRef.current !== null) cancelAnimationFrame(revealRafRef.current);
    },
    [],
  );

  // Fallback reveal: if the diff-update event never arrives (e.g. an identical
  // before/after that fires nothing, or a slow compute), don't leave the diff
  // hidden — fade it in after a short grace period.
  useEffect(() => {
    if (!mounted || diffSettled) return;
    const timer = setTimeout(() => setDiffSettled(true), 600);
    return () => clearTimeout(timer);
  }, [mounted, diffSettled]);

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
    // Auto-height (the stacked GitHub overview) is a read-only view — no
    // inline-comment affordance.
    canComment: canEdit && !autoHeight,
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
      // In auto-height mode the editor is exactly its content height, so there
      // is nothing to scroll inside it — hide its scrollbar and let the wheel
      // bubble to the page so many stacked diffs scroll as one. Also tighten
      // the left gutter: an inline diff reserves two line-number columns, and
      // Monaco's defaults (lineNumbersMinChars 5, plus a folding column and a
      // glyph margin) leave a wide static band — oversized in the narrow rail
      // and for short files. Trim the reserved digits and drop the folding /
      // glyph columns the stacked overview doesn't use.
      ...(autoHeight
        ? {
            scrollbar: {
              vertical: "hidden" as const,
              alwaysConsumeMouseWheel: false,
            },
            lineNumbersMinChars: 3,
            folding: false,
            glyphMargin: false,
            lineDecorationsWidth: 4,
          }
        : {}),
    };
  }, [layout, hideWhitespace, wrapLines, autoHeight]);

  return (
    <div className={cn("flex flex-col", !autoHeight && "h-full")}>
      <div
        className={cn("relative", !autoHeight && "min-h-0 flex-1")}
        // In auto-height mode the diff sizes to its content; a placeholder
        // height reserves space until the first measurement so the scroll
        // position doesn't jump as sections mount.
        style={autoHeight ? { height: contentHeight ?? AUTO_HEIGHT_PLACEHOLDER } : undefined}
      >
        {loadError ? (
          <div className="flex items-center justify-center p-8 text-destructive text-ui">
            Failed to load the diff.
          </div>
        ) : (
          <>
            {/* Mount once the grammar is ready so Monaco can compute the diff,
                but keep it hidden (opacity, not display — automaticLayout still
                needs to measure) until the unchanged-region collapse settles. */}
            {ready && (
              <div
                className={cn(
                  "h-full transition-opacity duration-150",
                  diffSettled ? "opacity-100" : "opacity-0",
                )}
              >
                <DiffEditor
                  height="100%"
                  theme={monacoTheme}
                  language={monacoLanguageId(lang)}
                  original={before ?? ""}
                  modified={after ?? ""}
                  options={options}
                  onMount={handleMount}
                />
              </div>
            )}
            {(!ready || !diffSettled) && (
              <div className="absolute inset-0 flex items-center justify-center p-8 text-muted-foreground text-ui">
                Loading diff…
              </div>
            )}
          </>
        )}
      </div>
      {commentButton}
    </div>
  );
}

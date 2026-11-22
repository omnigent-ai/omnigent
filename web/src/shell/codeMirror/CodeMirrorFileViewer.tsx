// File viewer and editor on CodeMirror. Only the visible part of the file is
// syntax highlighted, so files of any size open, scroll, and stay highlighted.
// Text selections anchor comments to offsets in the raw file. Find and replace,
// go to line, and multiple cursors work as in other code editors.

import {
  closeSearchPanel,
  getSearchQuery,
  highlightSelectionMatches,
  openSearchPanel,
  search,
  searchKeymap,
  searchPanelOpen,
} from "@codemirror/search";
import { Compartment, EditorState } from "@codemirror/state";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
  type Panel,
  scrollPastEnd,
} from "@codemirror/view";
import { reestimateWrappedHeightsOnSettledWidth } from "./wrappedHeightEstimate";
import { HashIcon, ReplaceIcon } from "lucide-react";
import { type ReactNode, type RefObject, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { FindBar } from "@/components/FindBar";
import { OWNS_COMMAND_ENTER_ATTRIBUTE } from "@/hooks/useApproveHotkey";
import { GO_TO_LINE_LABEL, GoToLineBar } from "@/components/GoToLineBar";
import { codeMirrorKeys } from "@/shell/workspaceTabs/workspaceSidebarShortcuts";
import { readCodeFont, subscribeCodeFont } from "@/lib/codeFontPreferences";
import { type PaletteCommand, usePaletteCommands } from "@/store/paletteCommands";
import type { FilePosition } from "../FileViewerContext";
import { detectLang } from "../codeViewerHelpers";
import { isFilePositionPending, stopFilePosition } from "../filePositionState";
import { TruncatedBanner } from "../TruncatedBanner";
import { attachEditorScrollRestore } from "../useScrollRestore";
import { citedLines, highlightCitedLines, revealCitedPosition } from "./citedLines";
import {
  applyFindQuery,
  FindMatchIndex,
  findQueryFromSelection,
  findStatusLabel,
  setReplaceText,
  stepToMatch,
  type FindStatus,
} from "./codeMirrorFind";
import {
  type GoToLineOrigin,
  goToLine,
  goToLineOrigin,
  mapGoToLineOrigin,
  previewLine,
  returnToOrigin,
} from "./codeMirrorGoToLine";
import { FindReplacer } from "./codeMirrorReplace";
import {
  codeMirrorFontTheme,
  codeMirrorViewerTheme,
  FIND_PANEL_CLASS,
  floatingFindPanelTheme,
} from "./codeMirrorTheme";
import { defaultMatchSource } from "./findMatchSource";
import {
  type CodeMirrorEditingCallbacks,
  type CodeMirrorEditorHandle,
  editability,
  editCommandAvailability,
  editorHandleFor,
} from "./codeMirrorEditing";
import { FILE_EDITOR_COMMANDS, fileEditorCommandLabel } from "@/components/fileEditorCommands";
import { baseEditorKeymap, cursorHistory, EDITOR_COMMAND_RUNS } from "./codeMirrorEditorCommands";
import { indentationFolding } from "./codeMirrorFolding";
import { detectedIndentation } from "./codeMirrorIndentation";
import { selectionWhitespacePlugin } from "./codeMirrorSelectionWhitespace";
import { documentRefresh } from "./documentRefresh";
import { copyRawSelection, rawDocument, rawDocumentOf } from "./rawOffsets";
import {
  type CodeMirrorCommentOptions,
  useCodeMirrorCommentLayer,
} from "./useCodeMirrorCommentLayer";
import { shikiDecorations } from "./shikiDecorations";
import { viewportHighlighting } from "./viewportHighlighting";

export const FIND_IN_FILE_LABEL = "Find in file";
export const FIND_AND_REPLACE_LABEL = "Find and replace in file";

// The file viewer routes Cmd+F to the find bar itself and steps matches through
// its index below; the editor keeps the other search keys (Mod-d, Escape).
// Go to line opens the viewer's own box instead of CodeMirror's unstyled dialog.
// Mod-d and Shift-Mod-l are editor commands, defined with the others.
const REPLACED_SEARCH_KEYS = new Set(["Mod-f", "F3", "Mod-g", "Mod-Alt-g", "Mod-d", "Shift-Mod-l"]);
const VIEWER_SEARCH_KEYMAP = searchKeymap.filter(
  (binding) => !REPLACED_SEARCH_KEYS.has(binding.key ?? ""),
);

// Input that means the reader has taken over from a citation's reveal.
const READER_INTERACTION_EVENTS = ["wheel", "touchstart", "pointerdown", "keydown"] as const;

const EMPTY_FIND_STATUS: FindStatus = {
  query: "",
  options: { caseSensitive: false, wholeWord: false, regexp: false },
  replace: "",
  valid: true,
  complete: true,
  matchCount: 0,
  ordinal: 0,
};

/** An open Go to line box; `request` changes each time it is asked for. */
interface GoToLineState {
  request: number;
}

interface PendingStep {
  direction: 1 | -1;
  from: { from: number; to: number };
  /** Accepts a match at `from` itself, as a new query's first match does. */
  inclusive: boolean;
}

export interface CodeMirrorFileViewerProps {
  conversationId: string;
  path: string;
  content: string;
  position?: FilePosition;
  truncated: boolean;
  /** Shows that a Markdown file opened as source because it is too large to render. */
  largeMarkdown?: boolean;
  wrapLines: boolean;
  searchOpen: boolean;
  /** Bumped on every find request so a repeat press refocuses the query. */
  searchFocusNonce?: number;
  /** Called when find closes from inside the viewer (Escape or ✕). */
  onSearchHandled: () => void;
  /** Called when the editor opens find itself, such as with F3 or Mod-g. */
  onSearchOpened: () => void;
  /** Comments on the file; omitted where the file can't take comments. */
  commentOptions?: Omit<CodeMirrorCommentOptions, "path">;
  /**
   * Makes the viewer an editor. Its owner then supplies newer content through
   * `editorRef` instead of `content`, so a refresh can't overwrite edits.
   */
  editing?: CodeMirrorEditing;
  /** Shown under the viewer's own banners, such as an editor's save conflict. */
  banner?: ReactNode;
}

export interface CodeMirrorEditing extends CodeMirrorEditingCallbacks {
  /** False while the file can't be edited, such as without permission. */
  editable: boolean;
  /** Why the file can't be edited, shown on its greyed-out edit commands in ⌘K. */
  readOnlyReason?: string;
  editorRef: RefObject<CodeMirrorEditorHandle | null>;
}

const NO_COMMENTS: Omit<CodeMirrorCommentOptions, "path"> = {
  comments: [],
  activeSelection: null,
  onSetActiveSelection: () => {},
  canComment: false,
};

export function CodeMirrorFileViewer({
  conversationId,
  path,
  content,
  position,
  truncated,
  largeMarkdown = false,
  wrapLines,
  searchOpen,
  searchFocusNonce,
  onSearchHandled,
  onSearchOpened,
  commentOptions = NO_COMMENTS,
  editing,
  banner,
}: CodeMirrorFileViewerProps) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [editorView, setEditorView] = useState<EditorView | null>(null);
  const [lineWrapping] = useState(() => new Compartment());
  const [font] = useState(() => new Compartment());
  const [editableCompartment] = useState(() => new Compartment());
  const editingRef = useRef(editing);
  editingRef.current = editing;
  const editable = editing?.editable ?? false;
  const commentLayer = useCodeMirrorCommentLayer(editorView, { ...commentOptions, path });
  const [findHost, setFindHost] = useState<HTMLElement | null>(null);
  const [findStatus, setFindStatus] = useState<FindStatus>(EMPTY_FIND_STATUS);
  const [findFocusRequest, setFindFocusRequest] = useState(0);
  const [findFocusTarget, setFindFocusTarget] = useState<"query" | "replace">("query");
  // Set by Find and replace before it opens find, for the open request to focus.
  const pendingFindFocusTargetRef = useRef<"query" | "replace" | null>(null);
  const [replaceOpen, setReplaceOpen] = useState(false);
  const [goToLineState, setGoToLineState] = useState<GoToLineState | null>(null);
  // Where Go to line returns to on Escape, kept in step with edits while it is open.
  const goToLineOriginRef = useRef<GoToLineOrigin | null>(null);
  const replacerRef = useRef<FindReplacer | null>(null);
  const [replacing, setReplacing] = useState(false);
  const cancelScrollRestoreRef = useRef<(() => void) | null>(null);
  const findIndexRef = useRef<FindMatchIndex | null>(null);
  // A step the index couldn't answer yet, retried as matches arrive. A query
  // edit queues one from the top to select the first match.
  const pendingStepRef = useRef<PendingStep | null>(null);
  const onSearchHandledRef = useRef(onSearchHandled);
  onSearchHandledRef.current = onSearchHandled;
  const onSearchOpenedRef = useRef(onSearchOpened);
  onSearchOpenedRef.current = onSearchOpened;
  const searchOpenRef = useRef(searchOpen);
  searchOpenRef.current = searchOpen;
  // Steps now when the index can answer, otherwise when it gets there.
  const requestStep = (target: EditorView, index: FindMatchIndex, direction: 1 | -1) => {
    const from = target.state.selection.main;
    pendingStepRef.current = stepToMatch(target, index, direction, from)
      ? null
      : { direction, from: { from: from.from, to: from.to }, inclusive: false };
  };
  const queueFirstMatch = (wanted: boolean) => {
    pendingStepRef.current = wanted
      ? { direction: 1, from: { from: 0, to: 0 }, inclusive: true }
      : null;
  };
  // The inputs a new view starts from; changes to a live view arrive through the effects below.
  const currentInputsRef = useRef({ content, wrapLines, position, editable });
  currentInputsRef.current = { content, wrapLines, position, editable };

  const openGoToLine = (target: EditorView) => {
    goToLineOriginRef.current ??= goToLineOrigin(target);
    setGoToLineState((current) => ({ request: (current?.request ?? 0) + 1 }));
    return true;
  };
  // A read-only file opens plain find, as ⌥⌘F would anywhere else.
  const openFindAndReplace = (target: EditorView) => {
    const canReplace = editingRef.current?.editable === true;
    if (canReplace) setReplaceOpen(true);
    const focusTarget = canReplace ? "replace" : "query";
    if (searchPanelOpen(target.state)) {
      setFindFocusTarget(focusTarget);
      setFindFocusRequest((request) => request + 1);
    } else {
      pendingFindFocusTargetRef.current = focusTarget;
      openSearchPanel(target);
    }
    return true;
  };
  // The editor's keymap is built once, so it reaches the latest handlers through refs.
  const openGoToLineRef = useRef(openGoToLine);
  openGoToLineRef.current = openGoToLine;
  const openFindAndReplaceRef = useRef(openFindAndReplace);
  openFindAndReplaceRef.current = openFindAndReplace;

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const initial = currentInputsRef.current;
    const language = detectLang(path);
    let tearingDown = false;
    let view: EditorView | undefined;
    const findIndex = new FindMatchIndex(() => {
      if (!view || !searchPanelOpen(view.state)) return;
      // A step waits for the index of the document it was asked in, as after a Replace.
      const pending = pendingStepRef.current;
      if (
        pending &&
        findIndex.belongsTo(view.state) &&
        stepToMatch(view, findIndex, pending.direction, pending.from, pending.inclusive)
      ) {
        pendingStepRef.current = null;
      }
      replacer.resume(view);
      setFindStatus(findIndex.status(view.state));
    }, defaultMatchSource());
    findIndexRef.current = findIndex;
    const replacer = new FindReplacer(findIndex, undefined, () => setReplacing(replacer.busy));
    replacerRef.current = replacer;
    const step = (direction: 1 | -1) => (target: EditorView) => {
      if (!searchPanelOpen(target.state)) openSearchPanel(target);
      else requestStep(target, findIndex, direction);
      return true;
    };
    const findPanel = (): Panel => {
      const dom = document.createElement("div");
      dom.className = FIND_PANEL_CLASS;
      return {
        dom,
        top: true,
        mount: () => {
          setFindHost(dom);
          // The toolbar's toggle must follow a panel the editor opened on its own.
          if (!searchOpenRef.current) onSearchOpenedRef.current();
        },
        destroy: () => {
          setFindHost(null);
          replacer.cancel();
          findIndex.reset();
          if (!tearingDown) onSearchHandledRef.current();
        },
      };
    };
    view = new EditorView({
      parent: host,
      state: EditorState.create({
        // CodeMirror splits "\r\n", "\r", and "\n" itself; the raw file keeps the original.
        doc: initial.content,
        extensions: [
          editableCompartment.of(editability(initial.editable, () => editingRef.current, path)),
          rawDocument.init(() => rawDocumentOf(initial.content)),
          copyRawSelection,
          commentLayer.extension,
          lineNumbers(),
          indentationFolding(),
          detectedIndentation(initial.content),
          selectionWhitespacePlugin,
          cursorHistory,
          drawSelection(),
          highlightActiveLine(),
          highlightActiveLineGutter(),
          // Room past the last line, so a citation of it can still center.
          scrollPastEnd(),
          citedLines,
          lineWrapping.of(initial.wrapLines ? EditorView.lineWrapping : []),
          reestimateWrappedHeightsOnSettledWidth,
          font.of(codeMirrorFontTheme(readCodeFont())),
          viewportHighlighting((text, from) => shikiDecorations(text, language, from)),
          search({
            top: true,
            createPanel: findPanel,
            scrollToMatch: (range) => EditorView.scrollIntoView(range, { y: "center" }),
          }),
          highlightSelectionMatches({ highlightWordAroundCursor: true, minSelectionLength: 1 }),
          EditorState.allowMultipleSelections.of(true),
          // Alt-click adds a cursor, as in other code editors.
          EditorView.clickAddsSelectionRange.of((event) => event.altKey),
          keymap.of([
            // Control+G is Go to line everywhere; on Windows and Linux it wins over find next.
            ...codeMirrorKeys("file.goToLine").map((key) => ({
              key,
              run: (target: EditorView) => openGoToLineRef.current(target),
              preventDefault: true,
            })),
            ...codeMirrorKeys("file.findAndReplace").map((key) => ({
              key,
              run: (target: EditorView) => openFindAndReplaceRef.current(target),
              preventDefault: true,
            })),
            { key: "Mod-g", run: step(1), shift: step(-1), preventDefault: true },
            { key: "F3", run: step(1), shift: step(-1), preventDefault: true },
            ...VIEWER_SEARCH_KEYMAP,
            ...baseEditorKeymap,
          ]),
          EditorView.updateListener.of((update) => {
            const origin = goToLineOriginRef.current;
            if (origin && update.docChanged) {
              goToLineOriginRef.current = mapGoToLineOrigin(origin, update.changes);
            }
          }),
          EditorView.updateListener.of((update) => {
            if (!searchPanelOpen(update.state)) return;
            const queryChanged = !getSearchQuery(update.startState).eq(
              getSearchQuery(update.state),
            );
            if (update.docChanged || queryChanged) {
              // Indexing may select the first match, which can't dispatch mid-update.
              queueMicrotask(() => {
                if (tearingDown) return;
                findIndex.sync(update.view.state);
                // A Replace all for the old document or query no longer applies.
                replacer.resume(update.view);
              });
            }
            if (update.docChanged || update.selectionSet || queryChanged) {
              setFindStatus(findIndex.status(update.state));
            }
          }),
          codeMirrorViewerTheme,
          floatingFindPanelTheme,
          EditorView.contentAttributes.of({ "aria-label": `Contents of ${path}` }),
        ],
      }),
    });
    const mountedView = view;
    viewRef.current = mountedView;
    setEditorView(mountedView);
    const editorRef = editingRef.current?.editorRef;
    if (editorRef) editorRef.current = editorHandleFor(mountedView);
    const scrollKey = `codemirror:${conversationId}:${path}`;
    cancelScrollRestoreRef.current = attachEditorScrollRestore(
      {
        setScrollTop: (top) => {
          mountedView.scrollDOM.scrollTop = top;
        },
        onDidScrollChange: (listener) => {
          const onScroll = () => listener({ scrollTop: mountedView.scrollDOM.scrollTop });
          mountedView.scrollDOM.addEventListener("scroll", onScroll, { passive: true });
          return { dispose: () => mountedView.scrollDOM.removeEventListener("scroll", onScroll) };
        },
        getDomNode: () => mountedView.dom,
      },
      () => scrollKey,
      () => viewRef.current === mountedView,
      !isFilePositionPending(initial.position),
    );
    const unsubscribeFont = subscribeCodeFont((next) =>
      mountedView.dispatch({ effects: font.reconfigure(codeMirrorFontTheme(next)) }),
    );
    return () => {
      tearingDown = true;
      replacer.cancel();
      replacerRef.current = null;
      findIndex.dispose();
      findIndexRef.current = null;
      unsubscribeFont();
      cancelScrollRestoreRef.current?.();
      cancelScrollRestoreRef.current = null;
      viewRef.current = null;
      setEditorView(null);
      if (editorRef) editorRef.current = null;
      mountedView.destroy();
    };
    // The comment extension is created once and reads its options through a ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId, path, lineWrapping, font, editableCompartment]);

  // A refreshed file keeps the reader's place outside the changed lines. An
  // editor's owner loads newer content itself, so edits aren't overwritten.
  useEffect(() => {
    const view = viewRef.current;
    if (!view || editingRef.current || view.state.field(rawDocument).text === content) return;
    view.dispatch(documentRefresh(view, content));
  }, [content]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: editableCompartment.reconfigure(
        editability(editable, () => editingRef.current, path),
      ),
    });
  }, [editable, editableCompartment, path]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: lineWrapping.reconfigure(wrapLines ? EditorView.lineWrapping : []),
    });
  }, [wrapLines, lineWrapping]);

  // Reveal a citation until the reader scrolls, clicks, or types; its lines
  // stay highlighted while it is the viewer's position.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    highlightCitedLines(view, position);
    if (!position || !isFilePositionPending(position)) return;
    cancelScrollRestoreRef.current?.();
    revealCitedPosition(view, position);
    // The pane can still be settling its size; keep the citation centered.
    const resize =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(() => {
            if (isFilePositionPending(position)) revealCitedPosition(view, position);
          });
    resize?.observe(view.scrollDOM);
    const stop = () => {
      stopFilePosition(position);
      resize?.disconnect();
    };
    for (const event of READER_INTERACTION_EVENTS) {
      view.dom.addEventListener(event, stop, { passive: true, capture: true });
    }
    return () => {
      resize?.disconnect();
      for (const event of READER_INTERACTION_EVENTS) {
        view.dom.removeEventListener(event, stop, { capture: true });
      }
    };
  }, [position]);

  // The toolbar's find toggle drives the panel. A request seeds the query from
  // a selection unless the reader is already typing in the bar.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    if (!searchOpen) {
      closeFind(view);
      return;
    }
    const barHasFocus =
      searchPanelOpen(view.state) &&
      view.dom.querySelector(`.${FIND_PANEL_CLASS}`)?.contains(document.activeElement);
    openSearchPanel(view);
    const selection = view.state.selection.main;
    if (!barHasFocus && !selection.empty) {
      queueFirstMatch(
        applyFindQuery(
          view,
          findQueryFromSelection(view.state.doc.sliceString(selection.from, selection.to)),
          findStatus.options,
        ),
      );
    }
    const findIndex = findIndexRef.current;
    if (findIndex) {
      findIndex.sync(view.state);
      setFindStatus(findIndex.status(view.state));
    }
    setFindFocusTarget(pendingFindFocusTargetRef.current ?? "query");
    pendingFindFocusTargetRef.current = null;
    setFindFocusRequest((request) => request + 1);
    // findStatus.options is read at request time only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchOpen, searchFocusNonce]);

  const paletteSource = `codemirror-file-viewer:${useId()}`;
  const onScreen = commentOptions.onScreen ?? true;
  const readOnlyReason = editing?.readOnlyReason;
  const editAvailability = useMemo(
    () => editCommandAvailability(editable, readOnlyReason),
    [editable, readOnlyReason],
  );
  const paletteCommands = useMemo<PaletteCommand[]>(() => {
    if (!onScreen || !editorView) return [];
    return [
      {
        id: "file.go-to-line",
        label: `${GO_TO_LINE_LABEL}…`,
        icon: HashIcon,
        keywords: ["jump", "line number", "column", "goto"],
        shortcut: "file.goToLine",
        movesFocus: true,
        run: () => openGoToLineRef.current(editorView),
      },
      {
        id: "file.find-and-replace",
        label: FIND_AND_REPLACE_LABEL,
        icon: ReplaceIcon,
        keywords: ["substitute", "replace all", "search"],
        shortcut: "file.findAndReplace" as const,
        movesFocus: true,
        availability: editAvailability,
        run: () => openFindAndReplaceRef.current(editorView),
      },
      ...FILE_EDITOR_COMMANDS.map((command) => ({
        id: command.id,
        label: fileEditorCommandLabel(command),
        icon: command.icon,
        keywords: command.keywords,
        shortcut: command.id,
        // The editor takes focus back, so the command acts where the reader left off.
        movesFocus: true,
        availability: command.edits ? editAvailability : undefined,
        run: () => {
          // The file may have become read-only since the palette listed the command.
          if (command.edits && !editingRef.current?.editable) return;
          editorView.focus();
          EDITOR_COMMAND_RUNS[command.id](editorView);
        },
      })),
    ];
  }, [onScreen, editorView, editAvailability]);
  usePaletteCommands(paletteSource, "File", paletteCommands);

  const view = viewRef.current;
  const findIndex = findIndexRef.current;
  const setQuery = (query: string, options = findStatus.options) => {
    if (view) queueFirstMatch(applyFindQuery(view, query, options));
  };
  const closeGoToLine = (restore: boolean) => {
    const origin = goToLineOriginRef.current;
    goToLineOriginRef.current = null;
    setGoToLineState(null);
    if (view && origin && restore) returnToOrigin(view, origin);
  };
  const replaceSelected = (target: EditorView, index: FindMatchIndex) => {
    replacerRef.current?.replaceSelected(target, (resumeFrom) => {
      // Not a match to replace: select the next one, so a replacement lands only on a highlighted match.
      if (resumeFrom === null) {
        requestStep(target, index, 1);
        return;
      }
      const from = { from: resumeFrom, to: resumeFrom };
      // A replacement that changed nothing leaves the index current; otherwise wait for the new one.
      if (index.belongsTo(target.state) && stepToMatch(target, index, 1, from)) return;
      pendingStepRef.current = { direction: 1, from, inclusive: false };
    });
  };
  return (
    <div className="flex h-full flex-col">
      {largeMarkdown && (
        <div className="shrink-0 border-b border-border bg-muted/50 px-4 py-2 text-sm text-muted-foreground">
          Large Markdown file opened as source to keep the viewer responsive.
        </div>
      )}
      {truncated && <TruncatedBanner />}
      {banner}
      <div className="relative min-h-0 flex-1">
        <div
          ref={hostRef}
          data-testid="codemirror-file-viewer"
          className="h-full [&>.cm-editor]:h-full"
          // ⌘↵ inserts a line below here, so a pending prompt's accept leaves the editor be.
          {...{ [OWNS_COMMAND_ENTER_ATTRIBUTE]: "" }}
        />
        {goToLineState && view && (
          <GoToLineBar
            lineCount={view.state.doc.lines}
            currentLine={view.state.doc.lineAt(view.state.selection.main.head).number}
            onPreview={(target) => {
              const origin = goToLineOriginRef.current;
              if (target) previewLine(view, target);
              else if (origin) view.scrollDOM.scrollTop = origin.scrollTop;
            }}
            onGo={(target) => {
              goToLineOriginRef.current = null;
              setGoToLineState(null);
              goToLine(view, target);
            }}
            onCancel={() => closeGoToLine(true)}
            onDismiss={() => closeGoToLine(false)}
            focusRequest={goToLineState.request}
            className="absolute top-2 left-1/2 z-10 -translate-x-1/2"
          />
        )}
      </div>
      {commentLayer.actions}
      {findHost &&
        view &&
        findIndex &&
        createPortal(
          <FindBar
            label={FIND_IN_FILE_LABEL}
            query={findStatus.query}
            onQueryChange={(query) => setQuery(query)}
            options={findStatus.options}
            onOptionsChange={(options) => setQuery(findStatus.query, options)}
            status={findStatusLabel(findStatus)}
            hasMatches={findStatus.matchCount > 0}
            searching={!findStatus.complete || replacing}
            onNext={() => requestStep(view, findIndex, 1)}
            onPrevious={() => requestStep(view, findIndex, -1)}
            onClose={() => {
              // Close the panel itself; the toolbar may already think find is closed.
              closeFind(view);
              onSearchHandled();
            }}
            focusRequest={findFocusRequest}
            focusTarget={findFocusTarget}
            replace={
              editable
                ? {
                    value: findStatus.replace,
                    onChange: (replace) => setReplaceText(view, replace),
                    open: replaceOpen,
                    onOpenChange: setReplaceOpen,
                    onReplace: () => replaceSelected(view, findIndex),
                    onReplaceAll: () => replacerRef.current?.replaceAll(view),
                  }
                : undefined
            }
            testIdPrefix="file-find"
          />,
          findHost,
        )}
    </div>
  );
}

function closeFind(view: EditorView): void {
  if (!searchPanelOpen(view.state)) return;
  closeSearchPanel(view);
  view.focus();
}

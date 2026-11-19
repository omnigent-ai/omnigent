// The file viewer's CodeMirror look, painted with the app's theme tokens so
// it follows light and dark mode and the code font preference.

import type { Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { codeFontFamilyForEditor, type CodeFont } from "@/lib/codeFontPreferences";
import "./codeMirrorFileViewer.css";

// Paints a line's background over the content margin on both sides; an outer shadow
// is drawn only outside the line's box, so a translucent colour doesn't double up.
const LINE_BACKGROUND_ACROSS_INSETS =
  "calc(-1 * var(--cm-line-inset-start)) 0 0 var(--cm-line-background), " +
  "var(--cm-line-inset-end) 0 0 var(--cm-line-background)";

export const codeMirrorViewerTheme: Extension = EditorView.theme({
  "&": {
    // Space between the text and the gutter or the right edge; line backgrounds reach across it.
    "--cm-line-inset-start": "8px",
    "--cm-line-inset-end": "14px",
    height: "100%",
    backgroundColor: "var(--background)",
    color: "var(--foreground)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    lineHeight: "1.55",
    overflow: "auto",
    overscrollBehavior: "contain",
    paddingTop: "8px",
  },
  // A margin, not line padding: CodeMirror estimates wrapped lines' heights from the
  // content's width, and padding inside each line made every estimate too short.
  ".cm-content": {
    padding: "0 0 96px 0",
    margin: "0 var(--cm-line-inset-end) 0 var(--cm-line-inset-start)",
    caretColor: "var(--foreground)",
  },
  ".cm-line": { padding: "0" },
  ".cm-gutters": {
    backgroundColor: "var(--background)",
    color: "var(--muted-foreground)",
    border: "none",
    paddingLeft: "6px",
  },
  ".cm-activeLine, .cm-activeLineGutter": {
    "--cm-line-background": "var(--viewer-active-line)",
    backgroundColor: "var(--cm-line-background)",
  },
  ".cm-activeLine, .cm-line.cm-cited-line": { boxShadow: LINE_BACKGROUND_ACROSS_INSETS },
  // The current line's number reads as text, so it stays clear on the active-line wash.
  ".cm-activeLineGutter": { color: "var(--foreground)" },
  // The code selection is the palette's, faded where syntax colours need it.
  ".cm-selectionLayer .cm-selectionBackground": {
    backgroundColor: "color-mix(in oklch, var(--code-selection) 72%, transparent)",
  },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground": {
    backgroundColor: "var(--code-selection)",
  },
  ".cm-searchMatch": {
    backgroundColor: "color-mix(in oklch, var(--warning) 30%, transparent)",
    borderRadius: "2px",
  },
  ".cm-searchMatch.cm-searchMatch-selected": {
    backgroundColor: "color-mix(in oklch, var(--warning) 70%, transparent)",
  },
  ".cm-selectionMatch": {
    backgroundColor: "color-mix(in oklch, var(--primary) 16%, transparent)",
  },
});

/** The class of the panel element that hosts the app's find bar in an editor. */
export const FIND_PANEL_CLASS = "cm-find-panel";

/** Places the find bar in an editor's top-right corner, above the text. */
export const findPanelTheme: Extension = EditorView.theme({
  ".cm-panels": { backgroundColor: "transparent", color: "inherit" },
  ".cm-panels-top": { border: "none" },
  // The bar takes clicks; the rest of its row lets them through to the text.
  [`.${FIND_PANEL_CLASS}`]: {
    display: "flex",
    justifyContent: "flex-end",
    padding: "8px 12px",
    pointerEvents: "none",
  },
});

/** Floats the find bar over the text instead of pushing it down, where the text is tall. */
export const floatingFindPanelTheme: Extension = [
  findPanelTheme,
  EditorView.theme({
    ".cm-panels-top": {
      position: "absolute",
      zIndex: "10",
      inset: "0 0 auto 0",
      height: "0",
      overflow: "visible",
      pointerEvents: "none",
    },
  }),
];

/** The code font preference as editor styles; reconfigured live when it changes. */
export function codeMirrorFontTheme(font: CodeFont): Extension {
  return EditorView.theme({
    ".cm-scroller": {
      fontFamily: codeFontFamilyForEditor(font.family),
      fontSize: `${font.sizePx}px`,
      fontWeight: String(font.weight),
    },
  });
}

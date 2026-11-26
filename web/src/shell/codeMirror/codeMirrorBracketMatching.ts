// Highlights the bracket that pairs with the one at the cursor, in read-only
// views as in the editor, painted with the app's theme tokens.

import { bracketMatching } from "@codemirror/language";
import type { Extension } from "@codemirror/state";
import { EditorView } from "@codemirror/view";

const matchedBracket = {
  backgroundColor: "color-mix(in oklch, var(--primary) 18%, transparent)",
  outline: "1px solid color-mix(in oklch, var(--primary) 45%, transparent)",
  borderRadius: "2px",
};

const unmatchedBracket = {
  backgroundColor: "transparent",
  color: "var(--destructive)",
  outline: "1px solid color-mix(in oklch, var(--destructive) 45%, transparent)",
  borderRadius: "2px",
};

export const matchingBrackets: Extension = [
  bracketMatching(),
  EditorView.theme({
    ".cm-matchingBracket, &.cm-focused .cm-matchingBracket": matchedBracket,
    ".cm-nonmatchingBracket, &.cm-focused .cm-nonmatchingBracket": unmatchedBracket,
  }),
];

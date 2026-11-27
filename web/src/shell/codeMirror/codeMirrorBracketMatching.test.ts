import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { afterEach, describe, expect, it } from "vitest";
import { matchingBrackets } from "./codeMirrorBracketMatching";

const views: EditorView[] = [];

afterEach(() => {
  for (const created of views.splice(0)) created.destroy();
});

function editorView(doc: string, cursor: number, readOnly: boolean): EditorView {
  const created = new EditorView({
    parent: document.body,
    state: EditorState.create({
      doc,
      selection: EditorSelection.single(cursor),
      extensions: [EditorState.readOnly.of(readOnly), matchingBrackets],
    }),
  });
  views.push(created);
  return created;
}

function highlighted(editor: EditorView, className: string): string[] {
  return [...editor.dom.querySelectorAll(`.${className}`)].map(
    (element) => element.textContent ?? "",
  );
}

describe("matchingBrackets", () => {
  it.each([true, false])("highlights both brackets of a pair (read-only: %s)", (readOnly) => {
    const editor = editorView("call(a, [b]) {\n}", 4, readOnly);
    expect(highlighted(editor, "cm-matchingBracket")).toEqual(["(", ")"]);
  });

  it("follows the cursor to another pair", () => {
    const editor = editorView("call(a, [b]) {\n}", 4, true);
    editor.dispatch({ selection: EditorSelection.single(8) });
    expect(highlighted(editor, "cm-matchingBracket")).toEqual(["[", "]"]);
  });

  it("marks a bracket without a partner", () => {
    const editor = editorView("call(a", 4, true);
    expect(highlighted(editor, "cm-nonmatchingBracket")).toEqual(["("]);
  });

  it("highlights nothing away from brackets", () => {
    const editor = editorView("call(a)", 1, true);
    expect(highlighted(editor, "cm-matchingBracket")).toEqual([]);
  });
});

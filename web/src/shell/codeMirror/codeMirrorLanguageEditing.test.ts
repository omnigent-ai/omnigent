import { EditorSelection, EditorState, Prec, Text } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
import { describe, expect, it } from "vitest";
import { foldedRanges, foldService } from "@codemirror/language";
import { indentationFolding, indentationFoldRange, opensIndentedBlock } from "./codeMirrorFolding";
import { detectIndentation } from "./codeMirrorIndentation";
import { detectLang, LANGUAGE_BY_EXTENSION } from "../codeViewerHelpers";
import {
  closesBracketsFor,
  commentTokensFor,
  editingLanguageFor,
  languageEditing,
  UNCOMMENTED_LANGUAGES,
} from "./codeMirrorLanguageEditing";
import { selectionWhitespacePlugin } from "./codeMirrorSelectionWhitespace";

const text = (content: string) => Text.of(content.split("\n"));

describe("detectIndentation", () => {
  it.each([
    { name: "two spaces", content: "a\n  b\n    c\n  d\ne", unit: "  ", tabSize: 2 },
    { name: "four spaces", content: "a\n    b\n        c\n    d\ne", unit: "    ", tabSize: 4 },
    { name: "tabs", content: "a\n\tb\n\t\tc\n\td", unit: "\t", tabSize: 4 },
    { name: "no indentation", content: "a\nb\nc", unit: "    ", tabSize: 4 },
    { name: "empty", content: "", unit: "    ", tabSize: 4 },
  ])("detects $name", ({ content, unit, tabSize }) => {
    expect(detectIndentation(text(content))).toEqual({ unit, tabSize });
  });

  it("ignores single-space alignment such as doc comment stars", () => {
    const content = "/**\n * one\n * two\n */\nfunction f() {\n    return 1;\n}";
    expect(detectIndentation(text(content)).unit).toBe("    ");
  });
});

describe("indentationFoldRange", () => {
  const state = EditorState.create({
    doc: "def f():\n    a = 1\n\n    return a\nprint(f())",
  });

  it("folds the deeper block after a line, across blank lines", () => {
    const range = indentationFoldRange(state, 0);
    expect(range).toEqual({ from: state.doc.line(1).to, to: state.doc.line(4).to });
  });

  it("has nothing to fold at a line without nested lines or at a blank line", () => {
    expect(indentationFoldRange(state, state.doc.line(5).from)).toBeNull();
    expect(indentationFoldRange(state, state.doc.line(3).from)).toBeNull();
    expect(indentationFoldRange(state, state.doc.line(2).from)).toBeNull();
  });
});

describe("indentation fold gutter", () => {
  it("marks lines that open a deeper block, looking past blank lines", () => {
    const state = EditorState.create({ doc: "a:\n\n  b\nc\n  \nd" });
    const opens = (number: number) => opensIndentedBlock(state, state.doc.line(number));
    expect([1, 2, 3, 4, 5, 6].map(opens)).toEqual([true, false, false, false, false, false]);
  });

  it("finds a block's end only when it is folded, not on every edit", () => {
    // Six nested headers over 250,000 deeper lines: scanning each block per edit froze typing.
    const headers = Array.from({ length: 6 }, (_, depth) => `${"  ".repeat(depth)}header ${depth}`);
    const doc = [...headers, ...Array.from({ length: 250_000 }, () => `${" ".repeat(12)}item`)];
    let rangeLookups = 0;
    const counting = Prec.highest(
      foldService.of(() => {
        rangeLookups += 1;
        return null;
      }),
    );
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({
        doc: doc.join("\n"),
        extensions: [counting, indentationFolding()],
      }),
    });
    try {
      expect(view.dom.querySelectorAll(".cm-foldMarker").length).toBeGreaterThan(0);
      for (let keystroke = 0; keystroke < 5; keystroke += 1) {
        view.dispatch({ changes: { from: view.state.doc.line(7).to, insert: "x" } });
      }
      expect(rangeLookups).toBe(0);

      const marker = view.dom.querySelector(".cm-foldGutter .cm-gutterElement:has(.cm-foldMarker)");
      marker?.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      expect(foldedRanges(view.state).size).toBe(1);
      expect(view.dom.querySelector(".cm-foldMarker-folded")).not.toBeNull();
    } finally {
      view.destroy();
    }
  });
});

describe("language editing data", () => {
  /** The comment tokens comment toggling reads in the file at `path`. */
  function commentsIn(path: string) {
    const state = EditorState.create({ extensions: languageEditing(path) });
    return state.languageDataAt<{ line?: string }>("commentTokens", 0)[0];
  }

  it.each([
    ["src/app.tsx", "//"],
    ["tool.py", "#"],
    ["query.sql", "--"],
    ["config.yaml", "#"],
    ["main.rs", "//"],
    ["settings.jsonc", "//"],
    ["config.json5", "//"],
    ["CMakeLists.txt", "#"],
    ["solver.jl", "#"],
    ["Makefile", "#"],
    ["Dockerfile", "#"],
  ])("comments lines in %s with %s", (path, line) => {
    expect(commentsIn(path)?.line).toBe(line);
  });

  it("has no comments in JSON, CSV, or plain text", () => {
    expect(commentsIn("package.json")).toBeUndefined();
    expect(commentsIn("data.csv")).toBeUndefined();
    expect(commentsIn("notes.txt")).toBeUndefined();
    expect(editingLanguageFor("package.json")).toBe("json");
    expect(editingLanguageFor("settings.jsonc")).toBe("jsonc");
  });

  it("knows the comment syntax of every language the viewer highlights", () => {
    const languages = new Set([
      ...Object.values(LANGUAGE_BY_EXTENSION),
      detectLang("Makefile"),
      detectLang("Dockerfile"),
      detectLang("CMakeLists.txt"),
    ]);
    for (const language of languages) {
      const known = commentTokensFor(language) !== undefined || UNCOMMENTED_LANGUAGES.has(language);
      expect(known, language).toBe(true);
    }
  });

  it("uses block comments for markup and CSS, and none for plain text", () => {
    expect(commentTokensFor("html")).toEqual({ block: { open: "<!--", close: "-->" } });
    expect(commentTokensFor("css")?.line).toBeUndefined();
    expect(commentTokensFor("text")).toBeUndefined();
  });

  it("closes brackets in code but not in prose", () => {
    expect(closesBracketsFor("typescript")).toBe(true);
    expect(closesBracketsFor("markdown")).toBe(false);
    expect(closesBracketsFor("text")).toBe(false);
  });
});

describe("selectionWhitespacePlugin", () => {
  it("marks spaces and tabs only inside the selection", () => {
    const view = new EditorView({
      parent: document.body,
      state: EditorState.create({
        doc: "a b\tc d",
        selection: EditorSelection.single(0, 4),
        extensions: selectionWhitespacePlugin,
      }),
    });
    expect(view.contentDOM.querySelectorAll(".cm-selectionSpace")).toHaveLength(1);
    expect(view.contentDOM.querySelectorAll(".cm-selectionTab")).toHaveLength(1);
    view.dispatch({ selection: EditorSelection.single(0) });
    expect(view.contentDOM.querySelectorAll(".cm-selectionSpace, .cm-selectionTab")).toHaveLength(
      0,
    );
    view.destroy();
  });
});

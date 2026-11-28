import { afterEach, describe, expect, it, vi } from "vitest";

import {
  applyUndoableTextEdit,
  formatMarkdownSelection,
  HEADING_LEVELS,
  headingLevelAt,
  inLineEndingsOf,
  isWebURL,
  MARKDOWN_FORMATS,
  type MarkdownEdit,
  markdownEditorShortcut,
  markdownLinkPaste,
  selectionHasFormat,
  setHeadingLevel,
} from "./markdownFormatting";

afterEach(() => {
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

/** Text with its selection written as `[` and `]`, or `|` for a caret. */
function edit(withSelection: string): MarkdownEdit {
  const [before = "", ...after] = withSelection.split("|");
  // A caret case keeps its brackets as text, so task lists can be written.
  if (after.length > 0) {
    const caret = before.length;
    return { value: before + after.join("|"), selectionStart: caret, selectionEnd: caret };
  }
  const value = withSelection.replace(/[[\]]/g, "");
  const start = withSelection.indexOf("[");
  return { value, selectionStart: start, selectionEnd: withSelection.indexOf("]") - 1 };
}

function marked({ value, selectionStart: start, selectionEnd: end }: MarkdownEdit): string {
  if (start === end) return `${value.slice(0, start)}|${value.slice(start)}`;
  return `${value.slice(0, start)}[${value.slice(start, end)}]${value.slice(end)}`;
}

const format = (text: string, name: Parameters<typeof formatMarkdownSelection>[1]) =>
  marked(formatMarkdownSelection(edit(text), name));

describe("formatMarkdownSelection", () => {
  it("wraps, and selects a placeholder when nothing is selected", () => {
    expect(format("a [b] c", "bold")).toBe("a **[b]** c");
    expect(format("a [b] c", "italic")).toBe("a _[b]_ c");
    expect(format("a [b] c", "strikethrough")).toBe("a ~~[b]~~ c");
    expect(format("a |c", "code")).toBe("a `[code]`c");
    expect(format("|", "link")).toBe("[[link text]](https://)");
    expect(format("see [docs]", "link")).toBe("see [[docs]](https://)");
  });

  it("unwraps markers inside or just outside the selection", () => {
    expect(format("a [**b**] c", "bold")).toBe("a [b] c");
    expect(format("a **[b]** c", "bold")).toBe("a [b] c");
    expect(format("[`x`]", "code")).toBe("[x]");
  });

  it("wraps a selection that is only the markers instead of unwrapping it", () => {
    expect(format("[**]", "bold")).toBe("**[**]**");
  });

  it("toggles a line prefix on every touched line", () => {
    expect(format("[one\ntwo]\nthree", "quote")).toBe("[> one\n> two]\nthree");
    expect(format("[> one\n> two]", "quote")).toBe("[one\ntwo]");
    expect(format("ti|tle", "heading")).toBe("[## title]");
    expect(format("[## title]", "heading")).toBe("[title]");
    expect(format("[# title]", "heading")).toBe("[title]");
  });

  it("numbers an ordered list and swaps one list kind for another", () => {
    expect(format("[a\nb\nc]", "ordered-list")).toBe("[1. a\n2. b\n3. c]");
    expect(format("[- a\n- b]", "ordered-list")).toBe("[1. a\n2. b]");
    expect(format("[1. a\n2. b]", "task-list")).toBe("[- [ ] a\n- [ ] b]");
    expect(format("- [x] a|", "unordered-list")).toBe("[- a]");
    expect(format("- [ ] a|", "task-list")).toBe("[a]");
  });

  it("formats an empty first line when the caret is at the very start", () => {
    expect(format("|\nbody", "heading")).toBe("[## ]\nbody");
    expect(format("|\nbody", "quote")).toBe("[> ]\nbody");
    expect(format("|\nbody", "unordered-list")).toBe("[- ]\nbody");
  });

  it("leaves the next line alone when the selection ends just after a newline", () => {
    expect(format("[one\n]two", "unordered-list")).toBe("[- one]\ntwo");
    expect(format("a\n[b\n]c", "quote")).toBe("a\n[> b]\nc");
  });

  it("fences a code block on its own line", () => {
    expect(format("text [code]", "code-block")).toBe("text \n```\n[code]\n```\n");
    expect(format("|", "code-block")).toBe("```\n|\n```\n");
  });
});

describe("markdownEditorShortcut", () => {
  const press = (init: Partial<KeyboardEventInit> & { key: string }) =>
    new KeyboardEvent("keydown", init);

  it("matches ⌘ on macOS and Control elsewhere", () => {
    expect(markdownEditorShortcut(press({ key: "b", code: "KeyB", metaKey: true }), true)).toBe(
      "bold",
    );
    expect(markdownEditorShortcut(press({ key: "b", code: "KeyB", ctrlKey: true }), false)).toBe(
      "bold",
    );
    expect(markdownEditorShortcut(press({ key: "b", code: "KeyB", ctrlKey: true }), true)).toBe(
      undefined,
    );
    expect(markdownEditorShortcut(press({ key: "b", code: "KeyB", metaKey: true }), false)).toBe(
      undefined,
    );
  });

  it("matches the Shift chords by physical key, whatever character they type", () => {
    const shifted = (code: string, key: string) =>
      markdownEditorShortcut(press({ key, code, metaKey: true, shiftKey: true }), true);
    expect(shifted("Digit7", "&")).toBe("ordered-list");
    expect(shifted("Digit8", "*")).toBe("unordered-list");
    expect(shifted("Period", ">")).toBe("quote");
    expect(shifted("KeyX", "X")).toBe("strikethrough");
    expect(shifted("KeyP", "P")).toBe("toggle-preview");
  });

  it("rejects extra modifiers, IME composition, and ⌘K (the palette)", () => {
    expect(
      markdownEditorShortcut(press({ key: "b", code: "KeyB", metaKey: true, altKey: true }), true),
    ).toBe(undefined);
    expect(
      markdownEditorShortcut(press({ key: "b", code: "KeyB", metaKey: true, ctrlKey: true }), true),
    ).toBe(undefined);
    expect(
      markdownEditorShortcut(
        press({ key: "b", code: "KeyB", metaKey: true, shiftKey: true }),
        true,
      ),
    ).toBe(undefined);
    expect(
      markdownEditorShortcut(
        press({ key: "b", code: "KeyB", metaKey: true, isComposing: true }),
        true,
      ),
    ).toBe(undefined);
    expect(markdownEditorShortcut(press({ key: "k", code: "KeyK", metaKey: true }), true)).toBe(
      undefined,
    );
  });

  it("gives Link no chord and keeps the toolbar to the nine reference formats", () => {
    expect(MARKDOWN_FORMATS.find((command) => command.format === "link")?.chord).toBeUndefined();
    expect(
      MARKDOWN_FORMATS.filter((command) => command.toolbar).map((command) => command.format),
    ).toEqual([
      "heading",
      "bold",
      "italic",
      "code",
      "link",
      "quote",
      "unordered-list",
      "ordered-list",
      "task-list",
    ]);
  });
});

describe("markdownLinkPaste", () => {
  it("links a selection to a pasted web URL and selects the text", () => {
    expect(marked(markdownLinkPaste(edit("see [the docs] here"), " https://x.dev/a ")!)).toBe(
      "see [[the docs]](https://x.dev/a) here",
    );
  });

  it("pastes normally with no selection, a non-web URL, or more than a URL", () => {
    expect(markdownLinkPaste(edit("see |"), "https://x.dev")).toBeUndefined();
    expect(markdownLinkPaste(edit("[a]"), "javascript:alert(1)")).toBeUndefined();
    expect(markdownLinkPaste(edit("[a]"), "mailto:a@b.c")).toBeUndefined();
    expect(markdownLinkPaste(edit("[a]"), "https://x.dev and more")).toBeUndefined();
    expect(markdownLinkPaste(edit("[a]"), "not a url")).toBeUndefined();
  });

  it("accepts only http and https", () => {
    expect(isWebURL("http://a.b")).toBe(true);
    expect(isWebURL("https://a.b/c?d")).toBe(true);
    expect(isWebURL("ftp://a.b")).toBe(false);
    expect(isWebURL("/relative")).toBe(false);
  });
});

describe("applyUndoableTextEdit", () => {
  function textarea(value: string): HTMLTextAreaElement {
    const element = document.createElement("textarea");
    element.value = value;
    document.body.append(element);
    return element;
  }

  it("inserts only the changed span through insertText, then sets the selection", () => {
    const element = textarea("hello world");
    const execCommand = vi.fn((_command: string, _ui: boolean, text: string) => {
      element.setRangeText(text, element.selectionStart, element.selectionEnd, "end");
      return true;
    });
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });
    applyUndoableTextEdit(element, {
      value: "hello **world**",
      selectionStart: 8,
      selectionEnd: 13,
    });
    // "world" ends both, so only "**world**" over "world" is inserted.
    expect(execCommand).toHaveBeenCalledWith("insertText", false, "**world**");
    expect(element.value).toBe("hello **world**");
    expect([element.selectionStart, element.selectionEnd]).toEqual([8, 13]);
  });

  it("falls back to an edit plus an input event when insertText does nothing", () => {
    const element = textarea("abc");
    Object.defineProperty(document, "execCommand", { value: () => false, configurable: true });
    const onInput = vi.fn();
    element.addEventListener("input", onInput);
    applyUndoableTextEdit(element, { value: "aXc", selectionStart: 2, selectionEnd: 2 });
    expect(element.value).toBe("aXc");
    expect(onInput).toHaveBeenCalledTimes(1);
  });

  it("never applies the edit twice when the native insert already made it", () => {
    const element = textarea("abc");
    Object.defineProperty(document, "execCommand", {
      value: (_command: string, _ui: boolean, text: string) => {
        element.setRangeText(text, element.selectionStart, element.selectionEnd, "end");
        return false;
      },
      configurable: true,
    });
    applyUndoableTextEdit(element, { value: "abXc", selectionStart: 3, selectionEnd: 3 });
    expect(element.value).toBe("abXc");
  });

  it("deletes a removed span", () => {
    const element = textarea("a**b**c");
    const execCommand = vi.fn((command: string) => {
      if (command === "delete")
        element.setRangeText("", element.selectionStart, element.selectionEnd);
      return true;
    });
    Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true });
    applyUndoableTextEdit(element, { value: "a**c", selectionStart: 3, selectionEnd: 3 });
    expect(execCommand).toHaveBeenCalledWith("delete");
    expect(element.value).toBe("a**c");
  });
});

describe("inLineEndingsOf", () => {
  it("returns the loaded bytes for unchanged text, mixed line endings included", () => {
    expect(inLineEndingsOf("a\r\nb\nc", "a\nb\nc")).toBe("a\r\nb\nc");
  });

  it("writes an edit of a CRLF body in CRLF", () => {
    expect(inLineEndingsOf("a\r\nb\r\n", "a\nb\nc\n")).toBe("a\r\nb\r\nc\r\n");
  });

  it("keeps LF for LF and mixed bodies, and for an empty start", () => {
    expect(inLineEndingsOf("a\nb", "a\nb\nc")).toBe("a\nb\nc");
    expect(inLineEndingsOf("a\r\nb\nc", "a\nb\nc!")).toBe("a\nb\nc!");
    expect(inLineEndingsOf("", "x\ny")).toBe("x\ny");
  });
});

describe("formatMarkdownSelection with the composer's options", () => {
  const word = { emptySelection: "word" } as const;

  it("keeps the toolbar's selected placeholder by default", () => {
    expect(marked(formatMarkdownSelection(edit("a | b"), "bold"))).toBe("a **[bold text]** b");
    expect(marked(formatMarkdownSelection(edit("a | b"), "italic"))).toBe("a _[italic text]_ b");
  });

  it("wraps the word at the caret, the caret keeping its place in it", () => {
    expect(marked(formatMarkdownSelection(edit("say hel|lo now"), "bold", word))).toBe(
      "say **hel|lo** now",
    );
    expect(marked(formatMarkdownSelection(edit("say |hello"), "italic", word))).toBe(
      "say _|hello_",
    );
  });

  it("unwraps a word that already has the markers", () => {
    expect(marked(formatMarkdownSelection(edit("say **hel|lo** now"), "bold", word))).toBe(
      "say hel|lo now",
    );
  });

  it("inserts empty markers between words, with the caret inside", () => {
    expect(marked(formatMarkdownSelection(edit("a | b"), "italic", word))).toBe("a _|_ b");
    expect(marked(formatMarkdownSelection(edit("|"), "bold", word))).toBe("**|**");
  });

  it("selects a new link's URL so typing replaces it", () => {
    const linked = formatMarkdownSelection(edit("see [docs] here"), "link", {
      linkSelection: "url",
    });
    expect(linked.value).toBe("see [docs](https://) here");
    expect(linked.value.slice(linked.selectionStart, linked.selectionEnd)).toBe("https://");
  });
});

describe("selectionHasFormat", () => {
  it("sees markers around or inside the selection", () => {
    expect(selectionHasFormat(edit("**[bold]**"), "bold")).toBe(true);
    expect(selectionHasFormat(edit("[**bold**]"), "bold")).toBe(true);
    expect(selectionHasFormat(edit("[plain]"), "bold")).toBe(false);
    expect(selectionHasFormat(edit("_[it]_"), "italic")).toBe(true);
  });

  it("doesn't read bold as italic", () => {
    expect(selectionHasFormat(edit("**[bold]**"), "italic")).toBe(false);
  });
});

describe("heading levels", () => {
  it("lists Normal Text first, then Heading 1 to 3", () => {
    expect(HEADING_LEVELS.map((heading) => [heading.level, heading.label])).toEqual([
      [0, "Normal Text"],
      [1, "Heading 1"],
      [2, "Heading 2"],
      [3, "Heading 3"],
    ]);
  });

  it("reads the caret line's level, levels past 3 as 3", () => {
    expect(headingLevelAt("plain", 2)).toBe(0);
    expect(headingLevelAt("a\n## Two", 4)).toBe(2);
    expect(headingLevelAt("##### Five", 3)).toBe(3);
    expect(headingLevelAt("#nospace", 3)).toBe(0);
  });

  it("sets a level and keeps the same text selected", () => {
    expect(marked(setHeadingLevel(edit("say [hello] there"), 2))).toBe("## say [hello] there");
    expect(marked(setHeadingLevel(edit("## say [hello]"), 1))).toBe("# say [hello]");
    expect(marked(setHeadingLevel(edit("### say [hello]"), 0))).toBe("say [hello]");
  });

  it("sets every selected line, moving a selection that ends on a later line", () => {
    expect(marked(setHeadingLevel(edit("[one\ntw]o"), 3))).toBe("### [one\n### tw]o");
  });

  it("moves a caret inside the old marker to the start of the text", () => {
    expect(marked(setHeadingLevel(edit("#|# title"), 1))).toBe("# |title");
  });
});

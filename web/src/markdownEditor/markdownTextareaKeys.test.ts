import { afterEach, describe, expect, it, vi } from "vitest";

import { createSharedKillRing } from "./killRing";
import { applyMarkdownFormat, handleMarkdownTextareaKey } from "./markdownTextareaKeys";

afterEach(() => document.body.replaceChildren());

/** A textarea holding `value`, with the selection written as `[` and `]` (or `|` for a caret). */
function textarea(withSelection: string): HTMLTextAreaElement {
  const element = document.createElement("textarea");
  document.body.append(element);
  const caret = withSelection.indexOf("|");
  const value = withSelection.replace(/[[\]|]/g, "");
  element.value = value;
  if (caret !== -1) element.setSelectionRange(caret, caret);
  else element.setSelectionRange(withSelection.indexOf("["), withSelection.indexOf("]") - 1);
  return element;
}

/** The textarea's value with its selection marked the same way. */
function marked(element: HTMLTextAreaElement): string {
  const { value, selectionStart: start, selectionEnd: end } = element;
  if (start === end) return `${value.slice(0, start)}|${value.slice(start)}`;
  return `${value.slice(0, start)}[${value.slice(start, end)}]${value.slice(end)}`;
}

function key(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { cancelable: true, ...init });
}

describe("applyMarkdownFormat", () => {
  it("wraps the selection and keeps it selected", () => {
    const element = textarea("make [this] bold");
    applyMarkdownFormat(element, "bold");
    expect(marked(element)).toBe("make **[this]** bold");
  });

  it("unwraps when the markers sit just outside or inside the selection", () => {
    const outside = textarea("make **[this]** bold");
    applyMarkdownFormat(outside, "bold");
    expect(marked(outside)).toBe("make [this] bold");
    const inside = textarea("make [**this**] bold");
    applyMarkdownFormat(inside, "bold");
    expect(marked(inside)).toBe("make [this] bold");
  });

  it("selects a placeholder between new markers when nothing is selected", () => {
    const element = textarea("type |here");
    applyMarkdownFormat(element, "code");
    expect(marked(element)).toBe("type `[code]`here");
  });

  it("tells the form the value changed", () => {
    const element = textarea("[x]");
    const onInput = vi.fn();
    element.addEventListener("input", onInput);
    applyMarkdownFormat(element, "italic");
    expect(onInput).toHaveBeenCalled();
  });

  it("prefixes each touched line, numbering an ordered list", () => {
    const element = textarea("[one\ntwo]\nthree");
    applyMarkdownFormat(element, "ordered-list");
    expect(element.value).toBe("1. one\n2. two\nthree");
  });

  it("leaves the next line alone when the selection ends after a newline", () => {
    const element = textarea("[quote me\n]next");
    applyMarkdownFormat(element, "quote");
    expect(element.value).toBe("> quote me\nnext");
  });
});

describe("handleMarkdownTextareaKey", () => {
  const options = (extra = {}) => ({
    mac: true,
    onSubmit: vi.fn(),
    onCancel: vi.fn(),
    onTogglePreview: vi.fn(),
    killRing: createSharedKillRing(),
    ...extra,
  });

  it("maps the formatting chords onto Markdown syntax", () => {
    const element = textarea("[word]");
    const keys = options();
    expect(
      handleMarkdownTextareaKey(key({ code: "KeyB", key: "b", metaKey: true }), element, keys),
    ).toBe(true);
    expect(element.value).toBe("**word**");
    handleMarkdownTextareaKey(
      key({ code: "KeyX", key: "x", metaKey: true, shiftKey: true }),
      element,
      keys,
    );
    expect(element.value).toBe("**~~word~~**");
    handleMarkdownTextareaKey(
      key({ code: "Digit8", key: "*", metaKey: true, shiftKey: true }),
      element,
      keys,
    );
    expect(element.value).toBe("- **~~word~~**");
  });

  it("submits on ⌘↵ and cancels on Esc", () => {
    const element = textarea("|");
    const keys = options();
    expect(handleMarkdownTextareaKey(key({ key: "Enter", metaKey: true }), element, keys)).toBe(
      true,
    );
    expect(keys.onSubmit).toHaveBeenCalledTimes(1);
    expect(handleMarkdownTextareaKey(key({ key: "Escape" }), element, keys)).toBe(true);
    expect(keys.onCancel).toHaveBeenCalledTimes(1);
  });

  it("kills shell words and lines, joins consecutive kills and yanks them on macOS", () => {
    const element = textarea("git commit --amend|");
    const keys = options();
    const control = (letter: string) =>
      key({ code: `Key${letter}`, key: letter.toLowerCase(), ctrlKey: true });
    handleMarkdownTextareaKey(control("W"), element, keys);
    handleMarkdownTextareaKey(control("W"), element, keys);
    expect(marked(element)).toBe("git |");
    handleMarkdownTextareaKey(control("Y"), element, keys);
    expect(marked(element)).toBe("git commit --amend|");
    element.setSelectionRange(0, 0);
    handleMarkdownTextareaKey(control("K"), element, keys);
    expect(element.value).toBe("");
  });

  it("claims no Control letter off macOS and ignores keys while composing", () => {
    const element = textarea("text|");
    expect(
      handleMarkdownTextareaKey(
        key({ code: "KeyW", key: "w", ctrlKey: true }),
        element,
        options({ mac: false }),
      ),
    ).toBe(false);
    expect(element.value).toBe("text");
    expect(
      handleMarkdownTextareaKey(
        key({ code: "KeyB", key: "b", metaKey: true, isComposing: true }),
        element,
        options(),
      ),
    ).toBe(false);
  });
  it("matches the chords as Control off macOS", () => {
    const element = textarea("[word]");
    const keys = options({ mac: false });
    expect(
      handleMarkdownTextareaKey(key({ code: "KeyB", key: "b", ctrlKey: true }), element, keys),
    ).toBe(true);
    expect(element.value).toBe("**word**");
    expect(
      handleMarkdownTextareaKey(key({ code: "KeyB", key: "b", metaKey: true }), element, keys),
    ).toBe(false);
    expect(handleMarkdownTextareaKey(key({ key: "Enter", ctrlKey: true }), element, keys)).toBe(
      true,
    );
    expect(keys.onSubmit).toHaveBeenCalledTimes(1);
  });

  it("toggles Preview on ⇧⌘P and leaves ⌘B with Alt or Control alone", () => {
    const element = textarea("[word]");
    const keys = options();
    expect(
      handleMarkdownTextareaKey(
        key({ code: "KeyP", key: "p", metaKey: true, shiftKey: true }),
        element,
        keys,
      ),
    ).toBe(true);
    expect(keys.onTogglePreview).toHaveBeenCalledTimes(1);
    for (const extra of [{ altKey: true }, { ctrlKey: true }]) {
      expect(
        handleMarkdownTextareaKey(
          key({ code: "KeyB", key: "b", metaKey: true, ...extra }),
          element,
          keys,
        ),
      ).toBe(false);
    }
    expect(element.value).toBe("word");
  });
});

describe("kill runs", () => {
  it("starts a new kill after the caret moves", () => {
    const element = textarea("alpha beta gamma|");
    const killRing = createSharedKillRing();
    const keys = {
      mac: true,
      onSubmit: () => {},
      killRing,
    };
    const control = (letter: string) =>
      new KeyboardEvent("keydown", {
        code: `Key${letter}`,
        key: letter.toLowerCase(),
        ctrlKey: true,
      });
    handleMarkdownTextareaKey(control("W"), element, keys);
    element.setSelectionRange(5, 5);
    handleMarkdownTextareaKey(control("W"), element, keys);
    expect(element.value).toBe(" beta ");
    element.setSelectionRange(element.value.length, element.value.length);
    handleMarkdownTextareaKey(control("Y"), element, keys);
    expect(element.value).toBe(" beta alpha");
  });
});

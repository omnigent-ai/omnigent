import type { ClipboardEvent } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  insertComposerText,
  insertPastedText,
  registerComposerInsertAdjuster,
} from "./composerText";
import { separateMentionEdges } from "@/lib/composerMentions";

function textarea(value = "", selection: [number, number] = [value.length, value.length]) {
  const element = document.createElement("textarea");
  document.body.append(element);
  element.value = value;
  element.setSelectionRange(...selection);
  return element;
}

/** A paste carrying *text*, as the browser hands it to the composer's handler. */
function paste(target: HTMLTextAreaElement, text: string) {
  const preventDefault = vi.fn();
  const event = {
    clipboardData: { getData: (type: string) => (type === "text/plain" ? text : "") },
    currentTarget: target,
    preventDefault,
  } as unknown as ClipboardEvent<HTMLTextAreaElement>;
  return { event, preventDefault };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("insertPastedText", () => {
  it("replaces the selection with the pasted text as typed input", () => {
    const element = textarea("hello world", [6, 11]);
    const onInput = vi.fn();
    element.addEventListener("input", onInput);
    const { event, preventDefault } = paste(element, "there");

    expect(insertPastedText(event)).toBe(true);

    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(element.value).toBe("hello there");
    expect(onInput).toHaveBeenCalledTimes(1);
  });

  it("normalizes Windows and classic Mac line endings", () => {
    const element = textarea();

    insertPastedText(paste(element, "one\r\ntwo\rthree").event);

    expect(element.value).toBe("one\ntwo\nthree");
  });

  it("leaves an empty paste to the browser", () => {
    const element = textarea("draft");
    const { event, preventDefault } = paste(element, "");

    expect(insertPastedText(event)).toBe(false);

    expect(preventDefault).not.toHaveBeenCalled();
    expect(element.value).toBe("draft");
  });

  it("inserts up to 100 lines and leaves longer pastes to the browser", () => {
    const lines = (count: number) =>
      Array.from({ length: count }, (_, i) => `line ${i}`).join("\n");
    const element = textarea();

    expect(insertPastedText(paste(element, lines(100)).event)).toBe(true);
    expect(element.value).toBe(lines(100));

    element.value = "";
    const { event, preventDefault } = paste(element, lines(101));
    expect(insertPastedText(event)).toBe(false);
    expect(preventDefault).not.toHaveBeenCalled();
    expect(element.value).toBe("");
  });
});

describe("insertComposerText with a registered insert adjuster", () => {
  const tokens = ["@readme.md"];
  function adjustingTextarea(value: string, caret: number) {
    const element = textarea(value, [caret, caret]);
    registerComposerInsertAdjuster(element, (text, insert) =>
      separateMentionEdges(text, insert, tokens),
    );
    return element;
  }
  /** Records the caret each `input` listener sees, as a React onChange handler would. */
  function recordCaretAtInput(element: HTMLTextAreaElement) {
    const seen: { value: string; caret: number }[] = [];
    element.addEventListener("input", () =>
      seen.push({ value: element.value, caret: element.selectionStart }),
    );
    return seen;
  }
  function stubExecCommand(stub: ((...args: unknown[]) => boolean) | undefined) {
    Object.defineProperty(document, "execCommand", { configurable: true, value: stub });
  }
  afterEach(() => {
    delete (document as { execCommand?: unknown }).execCommand;
  });

  it("adds the space and places the caret before input fires, when execCommand is unavailable", () => {
    const element = adjustingTextarea("Read @readme.md", 15);
    const seen = recordCaretAtInput(element);

    expect(insertComposerText(element, "x")).toBe(true);

    expect(element.value).toBe("Read @readme.md x");
    expect(element.selectionStart).toBe(17);
    expect(seen).toEqual([{ value: "Read @readme.md x", caret: 17 }]);
  });

  it("places the caret after the typed text when a space is added before a token", () => {
    const element = adjustingTextarea("@readme.md", 0);
    const seen = recordCaretAtInput(element);

    insertComposerText(element, "x");

    expect(element.value).toBe("x @readme.md");
    expect(element.selectionStart).toBe(1);
    expect(seen).toEqual([{ value: "x @readme.md", caret: 1 }]);
  });

  it("inserts the adjusted text once through a browser command, placing the caret before input", () => {
    const element = adjustingTextarea("Read @readme.md", 15);
    const commands: unknown[][] = [];
    const seen = recordCaretAtInput(element);
    stubExecCommand((...args: unknown[]) => {
      commands.push(args);
      // Browser behaviour: insert at the selection, leave the caret at its end, then fire input.
      element.setRangeText(String(args[2]), element.selectionStart, element.selectionEnd, "end");
      element.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    });

    insertComposerText(element, "x");

    expect(commands).toEqual([["insertText", false, " x"]]);
    expect(element.value).toBe("Read @readme.md x");
    expect(seen).toEqual([{ value: "Read @readme.md x", caret: 17 }]);
  });

  it("does not insert twice when execCommand reports failure after inserting", () => {
    const element = adjustingTextarea("Read @readme.md", 15);
    stubExecCommand((...args: unknown[]) => {
      element.setRangeText(String(args[2]), element.selectionStart, element.selectionEnd, "end");
      return false;
    });

    insertComposerText(element, "x");

    expect(element.value).toBe("Read @readme.md x");
  });

  it("leaves an insert alone when no tag is at its edge", () => {
    const element = adjustingTextarea("Read more", 4);
    insertComposerText(element, "x");
    expect(element.value).toBe("Readx more");
  });
});

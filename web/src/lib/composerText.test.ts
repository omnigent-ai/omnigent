import type { ClipboardEvent } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { insertPastedText } from "./composerText";

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

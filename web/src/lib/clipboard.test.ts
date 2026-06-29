import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText } from "./clipboard";

const clipboardDescriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, "clipboard");
const execCommandDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "execCommand");

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  document.getSelection()?.removeAllRanges();

  if (clipboardDescriptor) {
    Object.defineProperty(Navigator.prototype, "clipboard", clipboardDescriptor);
  } else {
    delete (Navigator.prototype as { clipboard?: unknown }).clipboard;
  }

  if (execCommandDescriptor) {
    Object.defineProperty(Document.prototype, "execCommand", execCommandDescriptor);
  } else {
    delete (Document.prototype as { execCommand?: unknown }).execCommand;
  }
});

describe("copyText", () => {
  it("selects an off-screen textarea and writes exact text through the fallback copy event", async () => {
    const setData = vi.fn();
    const selectedTextAreas: HTMLTextAreaElement[] = [];
    const originalSelect = HTMLTextAreaElement.prototype.select;
    const originalFocus = HTMLTextAreaElement.prototype.focus;
    const selectedRange = document.createRange();
    const existingText = document.createTextNode("existing selection");
    const selectionContainer = document.createElement("p");

    selectionContainer.appendChild(existingText);
    document.body.appendChild(selectionContainer);
    selectedRange.selectNodeContents(existingText);
    document.getSelection()?.removeAllRanges();
    document.getSelection()?.addRange(selectedRange);

    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: undefined,
    });
    vi.spyOn(HTMLTextAreaElement.prototype, "focus").mockImplementation(function focus(
      this: HTMLTextAreaElement,
    ) {
      originalFocus.call(this);
    });
    vi.spyOn(HTMLTextAreaElement.prototype, "select").mockImplementation(function select(
      this: HTMLTextAreaElement,
    ) {
      selectedTextAreas.push(this);
      originalSelect.call(this);
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        expect(selectedTextAreas).toHaveLength(1);
        expect(selectedTextAreas[0]?.value).toBe("first line\nsecond line");
        expect(selectedTextAreas[0]?.selectionStart).toBe(0);
        expect(selectedTextAreas[0]?.selectionEnd).toBe("first line\nsecond line".length);
        expect(document.body.contains(selectedTextAreas[0] ?? null)).toBe(true);

        const event = new Event("copy", {
          bubbles: true,
          cancelable: true,
        }) as ClipboardEvent;
        Object.defineProperty(event, "clipboardData", {
          configurable: true,
          value: { setData },
        });
        document.dispatchEvent(event);
        return true;
      }),
    });

    await expect(copyText("first line\nsecond line")).resolves.toBeUndefined();

    expect(setData).toHaveBeenCalledTimes(1);
    expect(setData).toHaveBeenCalledWith("text/plain", "first line\nsecond line");
    expect(document.querySelector("textarea")).toBeNull();
    expect(document.getSelection()?.rangeCount).toBe(1);
    expect(document.getSelection()?.getRangeAt(0)).toBe(selectedRange);
  });

  it("falls back to selected-textarea copy when async clipboard rejects", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("permission denied"));
    const selectedTextAreas: HTMLTextAreaElement[] = [];

    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    vi.spyOn(HTMLTextAreaElement.prototype, "select").mockImplementation(function select(
      this: HTMLTextAreaElement,
    ) {
      selectedTextAreas.push(this);
    });
    Object.defineProperty(Document.prototype, "execCommand", {
      configurable: true,
      value: vi.fn((command: string) => {
        expect(command).toBe("copy");
        expect(selectedTextAreas[0]?.value).toBe("fallback text");
        return true;
      }),
    });

    await expect(copyText("fallback text")).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith("fallback text");
    expect(document.execCommand).toHaveBeenCalledWith("copy");
  });
});

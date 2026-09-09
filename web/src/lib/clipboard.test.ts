import { afterEach, describe, expect, it, vi } from "vitest";

import { copyText, copyTextWithImage } from "./clipboard";

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
    const focusTarget = document.createElement("button");

    focusTarget.textContent = "keep focus";
    document.body.appendChild(focusTarget);
    focusTarget.focus();
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
    expect(document.getSelection()?.toString()).toBe("existing selection");
    expect(document.activeElement).toBe(focusTarget);
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

/** Records what it was constructed with, mirroring the real ClipboardItem
 *  enough for these tests: a `types` list and the raw per-type data. */
class StubClipboardItem {
  types: string[];
  data: Record<string, Blob | PromiseLike<Blob>>;
  constructor(data: Record<string, Blob | PromiseLike<Blob>>) {
    this.data = data;
    this.types = Object.keys(data);
  }
}

describe("copyTextWithImage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("writes one item with both types, calling write before the image settles", async () => {
    let resolveImage!: (blob: Blob) => void;
    const loadImage = vi.fn(
      () =>
        new Promise<Blob>((resolve) => {
          resolveImage = resolve;
        }),
    );
    const written: StubClipboardItem[] = [];
    const write = vi.fn((items: StubClipboardItem[]) => {
      written.push(...items);
      return Promise.resolve();
    });
    vi.stubGlobal("ClipboardItem", StubClipboardItem);
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { write },
    });

    const result = copyTextWithImage("a screenshot", loadImage);

    // The write must already have happened — proving it ran synchronously,
    // not after the image promise resolved.
    expect(write).toHaveBeenCalledTimes(1);
    expect(written).toHaveLength(1);
    expect(written[0]!.types).toEqual(["text/plain", "image/png"]);

    resolveImage(new Blob([], { type: "image/png" }));
    await expect(result).resolves.toBeUndefined();
    const image = await written[0]!.data["image/png"];
    expect(image?.type).toBe("image/png");
  });

  it("omits text/plain when there is no text", async () => {
    const written: StubClipboardItem[] = [];
    const write = vi.fn((items: StubClipboardItem[]) => {
      written.push(...items);
      return Promise.resolve();
    });
    vi.stubGlobal("ClipboardItem", StubClipboardItem);
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { write },
    });

    await expect(
      copyTextWithImage("", () => Promise.resolve(new Blob([], { type: "image/png" }))),
    ).resolves.toBeUndefined();

    expect(written[0]!.types).toEqual(["image/png"]);
  });

  it("falls back to writeText when ClipboardItem is unavailable", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const write = vi.fn();
    // ClipboardItem is left unstubbed — undefined in this environment.
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { write, writeText },
    });

    await expect(copyTextWithImage("fallback text", vi.fn())).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith("fallback text");
    expect(write).not.toHaveBeenCalled();
  });

  it("falls back to writeText when clipboard.write rejects", async () => {
    const write = vi.fn().mockRejectedValue(new Error("denied"));
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("ClipboardItem", StubClipboardItem);
    Object.defineProperty(Navigator.prototype, "clipboard", {
      configurable: true,
      value: { write, writeText },
    });

    await expect(
      copyTextWithImage("fallback text", () => Promise.resolve(new Blob())),
    ).resolves.toBeUndefined();

    expect(writeText).toHaveBeenCalledWith("fallback text");
  });
});

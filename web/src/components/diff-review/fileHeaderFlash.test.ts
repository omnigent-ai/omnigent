import { afterEach, describe, expect, it, vi } from "vitest";

import {
  FILE_HEADER_FLASH_ATTRIBUTE,
  FILE_HEADER_FLASH_MS,
  flashFileHeader,
} from "./fileHeaderFlash";

function renderedFile(withHeader: boolean): HTMLElement {
  const item = document.createElement("div");
  const root = item.attachShadow({ mode: "open" });
  if (withHeader) root.innerHTML = "<div data-diffs-header></div>";
  return item;
}

describe("flashFileHeader", () => {
  afterEach(() => vi.useRealTimers());

  it("marks the header in the file's shadow root, then clears it once the flash ends", () => {
    vi.useFakeTimers();
    const item = renderedFile(true);
    const header = item.shadowRoot!.querySelector("[data-diffs-header]")!;
    expect(flashFileHeader(item)).toBe(true);
    expect(header.hasAttribute(FILE_HEADER_FLASH_ATTRIBUTE)).toBe(true);
    vi.advanceTimersByTime(FILE_HEADER_FLASH_MS - 1);
    // A second pick restarts the flash rather than ending with the first one's timer.
    flashFileHeader(item);
    vi.advanceTimersByTime(FILE_HEADER_FLASH_MS - 1);
    expect(header.hasAttribute(FILE_HEADER_FLASH_ATTRIBUTE)).toBe(true);
    vi.advanceTimersByTime(1);
    expect(header.hasAttribute(FILE_HEADER_FLASH_ATTRIBUTE)).toBe(false);
  });

  it("reports a file whose header isn't rendered yet, so the caller tries again", () => {
    expect(flashFileHeader(renderedFile(false))).toBe(false);
  });
});

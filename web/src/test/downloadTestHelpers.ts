/** Shared spies for code that saves files through a synthetic `<a download>` click. */
import { vi } from "vitest";

/**
 * Record each synthetic download link's `[href, download]` instead of
 * navigating. The spy is undone by `vi.restoreAllMocks()`.
 */
export function captureDownloads(): [string, string][] {
  const clicked: [string, string][] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    clicked.push([this.getAttribute("href") ?? "", this.download]);
  });
  return clicked;
}

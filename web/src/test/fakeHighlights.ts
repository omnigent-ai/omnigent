// The CSS Custom Highlight API for tests: jsdom has none. Each Highlight keeps its ranges in
// order, so a test can read what a name paints.

import { vi } from "vitest";

export class FakeHighlight {
  readonly ranges: Range[];
  constructor(...ranges: Range[]) {
    this.ranges = ranges;
  }
  [Symbol.iterator]() {
    return this.ranges[Symbol.iterator]();
  }
}

/** Installs a page-wide highlight registry; undo with `vi.unstubAllGlobals()`. */
export function installFakeHighlights(): Map<string, FakeHighlight> {
  const highlights = new Map<string, FakeHighlight>();
  vi.stubGlobal("CSS", { ...globalThis.CSS, highlights });
  vi.stubGlobal("Highlight", FakeHighlight);
  return highlights;
}

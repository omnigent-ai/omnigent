import { describe, expect, it } from "vitest";

import { pullRequestFileCommands } from "./pullRequestFileCommands";

describe("pullRequestFileCommands", () => {
  it("offers the file on screen in a new tab where files can have their own tab", () => {
    expect(pullRequestFileCommands({ shownPath: "src/app.ts", newTab: true })).toEqual([
      {
        id: "open-file-in-new-tab",
        paletteLabel: "Open src/app.ts in a new tab",
        leadsSources: false,
        keywords: expect.arrayContaining(["file", "tab", "new"]),
        availability: { available: true },
        path: "src/app.ts",
      },
    ]);
  });

  it("offers nothing without a file on screen or outside the tabs sidebar", () => {
    expect(pullRequestFileCommands({ shownPath: null, newTab: true })).toEqual([]);
    expect(pullRequestFileCommands({ shownPath: "src/app.ts", newTab: false })).toEqual([]);
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { formatDividerDragEnd, reportDividerDragEnd } from "./paneDividerDragEnd";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("a divider drag's end line", () => {
  it("names the session, the handle and the reason", () => {
    expect(formatDividerDragEnd("conv_1", "corner", "pointer-lost")).toBe(
      "[pane-divider] drag ended unsaved session=conv_1 handle=corner reason=pointer-lost",
    );
  });

  it.each([
    ["a space", "conv 1"],
    ["a newline", "conv_1\nforged line"],
    ["nothing", ""],
    ["too many characters", "c".repeat(129)],
  ])("masks a session id with %s", (_case, conversationId) => {
    expect(formatDividerDragEnd(conversationId, "divider", "split-hidden")).toBe(
      "[pane-divider] drag ended unsaved session=invalid handle=divider reason=split-hidden",
    );
  });

  it("logs the line once, and never throws when the console does", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    reportDividerDragEnd("conv_1", "divider", "pane-maximized");
    expect(info).toHaveBeenCalledExactlyOnceWith(
      "[pane-divider] drag ended unsaved session=conv_1 handle=divider reason=pane-maximized",
    );
    info.mockImplementation(() => {
      throw new Error("console unavailable");
    });
    expect(() => reportDividerDragEnd("conv_1", "divider", "divider-gone")).not.toThrow();
  });
});

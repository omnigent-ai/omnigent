import { afterEach, describe, expect, it } from "vitest";
import {
  begin,
  closeTool,
  editDrafts,
  isCurrent,
  openTool,
  resetDesignTools,
  settle,
  tabTools,
  toggleTool,
} from "./designToolsStore";

const TAB = "browser-tab:conv_1:tab_1";
const OTHER = "browser-tab:conv_1:tab_2";

afterEach(resetDesignTools);

describe("design tools store", () => {
  it("opens one tool per tab with fresh drafts, and toggles it closed", () => {
    openTool(TAB, "annotate");
    editDrafts(TAB, { request: "Make it green" });
    openTool(TAB, "draw");
    expect(tabTools(TAB)).toMatchObject({ tool: "draw", request: "", phase: "editing" });
    expect(tabTools(OTHER).tool).toBeNull();
    toggleTool(TAB, "draw");
    expect(tabTools(TAB).tool).toBeNull();
  });

  it("changes drafts only while editing", () => {
    expect(editDrafts(TAB, { note: "no tool" })).toBe(false);
    openTool(TAB, "draw");
    expect(editDrafts(TAB, { note: "Bigger" })).toBe(true);
    const ticket = begin(TAB, "preparing")!;
    expect(editDrafts(TAB, { note: "Changed while capturing" })).toBe(false);
    expect(begin(TAB, "preparing")).toBeNull();
    expect(tabTools(TAB).note).toBe("Bigger");
    settle(ticket);
    expect(editDrafts(TAB, { note: "Again" })).toBe(true);
  });

  it("drops work from a tool session that has ended", () => {
    openTool(TAB, "draw");
    const ticket = begin(TAB, "preparing")!;
    // Closed meanwhile, and the same tool reopened: a new session.
    closeTool(TAB);
    openTool(TAB, "draw");
    expect(isCurrent(ticket)).toBe(false);
    expect(settle(ticket, { note: "Late" })).toBe(false);
    expect(tabTools(TAB)).toMatchObject({ tool: "draw", phase: "editing", note: "" });
  });

  it("refuses a late draft change from an ended session", () => {
    openTool(TAB, "draw");
    const ticket = { tabId: TAB, session: tabTools(TAB).session };
    expect(editDrafts(TAB, { note: "Now" }, ticket)).toBe(true);
    // Closed and reopened while a pointer was still down: the old stroke can't land.
    closeTool(TAB);
    openTool(TAB, "draw");
    expect(editDrafts(TAB, { note: "Stale" }, ticket)).toBe(false);
    expect(tabTools(TAB).note).toBe("");
  });

  it("closes at any phase", () => {
    openTool(TAB, "draw");
    begin(TAB, "preparing");
    closeTool(TAB);
    expect(tabTools(TAB)).toMatchObject({ tool: null, phase: "editing" });
  });
});

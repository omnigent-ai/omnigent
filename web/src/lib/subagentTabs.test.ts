import { describe, expect, it } from "vitest";
import { subagentSessionIdFromTab, subagentTabId } from "./subagentTabs";

describe("subagentTabs", () => {
  it("round-trips a sub-agent session id through its tab id", () => {
    const tabId = subagentTabId("conv_child");
    expect(tabId).not.toBe("conv_child");
    expect(subagentSessionIdFromTab(tabId)).toBe("conv_child");
  });

  it.each([
    ["a side chat", "conv_side"],
    ["a pending side chat", "pending:abc"],
    ["a prefix with no session id", subagentTabId("")],
  ])("returns null for %s", (_name, tabId) => {
    expect(subagentSessionIdFromTab(tabId)).toBeNull();
  });
});

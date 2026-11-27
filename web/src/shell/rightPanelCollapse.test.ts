import { describe, expect, it } from "vitest";

import { type RightPanelCollapseState, rightPanelCollapsesItself } from "./rightPanelCollapse";

// The width the floating strip measured in Chromium needs to show its Collapse button.
const CONTROLS = 154;

const floatingTabs: RightPanelCollapseState = {
  floatingPanesShown: true,
  tabsSidebar: true,
  tabsInDrawer: false,
  rightPanelOpen: true,
  railWidth: 360,
  controlsWidth: CONTROLS,
};

describe("rightPanelCollapsesItself", () => {
  it("hands collapsing to the open floating tabs sidebar's own button", () => {
    expect(rightPanelCollapsesItself(floatingTabs)).toBe(true);
  });

  it("keeps the corner handoff while the sidebar is collapsed", () => {
    expect(
      rightPanelCollapsesItself({ ...floatingTabs, rightPanelOpen: false, railWidth: 0 }),
    ).toBe(true);
  });

  it.each([
    ["without floating panes", { floatingPanesShown: false }],
    ["with the classic sidebar", { tabsSidebar: false }],
    ["in a narrow window's drawer", { tabsInDrawer: true }],
    ["before the sidebar's strip has laid out", { controlsWidth: null }],
  ])("leaves the header's toggle %s", (_case, overrides) => {
    expect(rightPanelCollapsesItself({ ...floatingTabs, ...overrides })).toBe(false);
  });

  // Narrow desktop windows with the left sidebar open leave the open sidebar 0, 12 or 92px.
  it.each([
    [0, false],
    [12, false],
    [92, false],
    [CONTROLS - 1, false],
    [CONTROLS, true],
    [CONTROLS + 1, true],
  ])("with a %ipx sidebar, hands collapsing to it: %s", (railWidth, handedOff) => {
    expect(rightPanelCollapsesItself({ ...floatingTabs, railWidth })).toBe(handedOff);
  });
});

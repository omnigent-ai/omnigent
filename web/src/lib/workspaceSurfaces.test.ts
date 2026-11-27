import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const tabsSidebar = vi.hoisted(() => ({ on: false }));
vi.mock("@/shell/useTabsSidebar", () => ({ useTabsSidebar: () => tabsSidebar.on }));
const serverFeatures = vi.hoisted(() => ({ values: {} as Record<string, boolean> }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ features: serverFeatures.values }),
}));

import {
  acceptedWorkspaceDesign,
  resetAcceptedWorkspaceDesignForTests,
  setAcceptedWorkspaceDesign,
  subscribeAcceptedWorkspaceDesign,
  usePullRequestsV2,
  useReportPullRequestsDesign,
} from "./workspaceSurfaces";

afterEach(() => {
  resetAcceptedWorkspaceDesignForTests();
  tabsSidebar.on = false;
  serverFeatures.values = {};
});

describe("Pull Requests v2", () => {
  it.each([
    { sidebar: false, flag: false, shown: false },
    { sidebar: false, flag: true, shown: false },
    { sidebar: true, flag: false, shown: false },
    { sidebar: true, flag: true, shown: true },
  ])("shows with the tabs sidebar and its own flag only: %o", ({ sidebar, flag, shown }) => {
    tabsSidebar.on = sidebar;
    serverFeatures.values = { pull_requests_v2: flag };
    expect(renderHook(() => usePullRequestsV2()).result.current).toBe(shown);
  });

  it("reports the design on screen, and again when the flag changes", () => {
    tabsSidebar.on = true;
    const { rerender } = renderHook(() => useReportPullRequestsDesign());
    expect(acceptedWorkspaceDesign()).toBe("classic");
    serverFeatures.values = { pull_requests_v2: true };
    rerender();
    expect(acceptedWorkspaceDesign()).toBe("tabs");
    serverFeatures.values = {};
    rerender();
    expect(acceptedWorkspaceDesign()).toBe("classic");
  });
});

describe("the accepted design", () => {
  it("tells subscribers about a change only", () => {
    const heard = vi.fn();
    const stop = subscribeAcceptedWorkspaceDesign(heard);
    act(() => setAcceptedWorkspaceDesign("tabs"));
    act(() => setAcceptedWorkspaceDesign("tabs"));
    expect(heard).toHaveBeenCalledTimes(1);
    expect(acceptedWorkspaceDesign()).toBe("tabs");
    stop();
    setAcceptedWorkspaceDesign("classic");
    expect(heard).toHaveBeenCalledTimes(1);
  });
});

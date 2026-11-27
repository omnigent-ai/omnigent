import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const tabsSidebar = vi.hoisted(() => ({ on: false }));
vi.mock("@/shell/useTabsSidebar", () => ({ useTabsSidebar: () => tabsSidebar.on }));
const serverFeatures = vi.hoisted(() => ({ values: {} as Record<string, boolean> }));
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ features: serverFeatures.values }),
}));

import { usePullRequestsV2 } from "./workspaceSurfaces";

afterEach(() => {
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
});

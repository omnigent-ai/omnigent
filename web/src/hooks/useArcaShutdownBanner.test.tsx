import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useArcaShutdownBanner } from "./useArcaShutdownBanner";
import { resetArcaWarningStorageForTests } from "@/lib/arcaShutdownWarning";

const MON_5PM = new Date(2026, 9, 5, 17, 0);
const TUE_5PM = new Date(2026, 9, 6, 17, 0);
const state = vi.hoisted(() => ({
  enabled: true,
  now: new Date(2026, 9, 5, 17, 0),
  hosts: [{ host_id: "arca", name: "jackson's arca", status: "online" }],
  hostsEnabled: true,
}));

vi.mock("@/hooks/useHosts", () => ({
  useHosts: ({ enabled }: { enabled: boolean }) => {
    state.hostsEnabled = enabled;
    return { data: state.hosts };
  },
}));
vi.mock("@/hooks/useNow", () => ({
  useNowSelector: (select: (now: Date) => string, { enabled }: { enabled: boolean }) =>
    enabled ? select(state.now) : "",
}));
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: () => ({}) }));
vi.mock("@/lib/capabilities", () => ({ isFeatureEnabled: () => state.enabled }));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(MON_5PM);
  localStorage.clear();
  resetArcaWarningStorageForTests();
  state.enabled = true;
  state.now = MON_5PM;
  state.hosts = [{ host_id: "arca", name: "jackson's arca", status: "online" }];
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("useArcaShutdownBanner", () => {
  it("disables hosts and eligibility when the feature is off", () => {
    state.enabled = false;
    const view = renderHook(() => useArcaShutdownBanner());
    expect(state.hostsEnabled).toBe(false);
    expect(view.result.current.showForHost("arca")).toBe(false);
    state.enabled = true;
    state.hosts = [{ ...state.hosts[0], status: "offline" }];
    view.rerender();
    expect(view.result.current.showForHost("arca")).toBe(false);
  });

  it("dismisses the banner until the next day, then offers it again", () => {
    const view = renderHook(() => useArcaShutdownBanner());
    expect(view.result.current.showForHost("arca")).toBe(true);
    act(() => view.result.current.dismissToday());
    expect(view.result.current.showForHost("arca")).toBe(false);
    state.now = TUE_5PM;
    vi.setSystemTime(TUE_5PM);
    view.rerender();
    expect(view.result.current.showForHost("arca")).toBe(true);
  });

  it("stays hidden after opt out", () => {
    const view = renderHook(() => useArcaShutdownBanner());
    act(() => view.result.current.optOut());
    state.now = TUE_5PM;
    vi.setSystemTime(TUE_5PM);
    view.rerender();
    expect(view.result.current.showForHost("arca")).toBe(false);
  });
});

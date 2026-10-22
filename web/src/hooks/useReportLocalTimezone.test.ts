import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useReportLocalTimezone } from "./useReportLocalTimezone";

const identity = vi.hoisted(() => ({ ready: false, viewer: null as string | null }));
const reportLocalTimezone = vi.hoisted(() => vi.fn(async () => {}));

vi.mock("@/hooks/useViewerId", () => ({
  useIdentityReady: () => identity.ready,
  useViewerId: () => identity.viewer,
}));
vi.mock("@/lib/scheduledTasksApi", () => ({ reportLocalTimezone }));

beforeEach(() => {
  identity.ready = false;
  identity.viewer = null;
  reportLocalTimezone.mockClear();
});

describe("useReportLocalTimezone", () => {
  it("waits for identity to settle before reporting", () => {
    const { rerender } = renderHook(() => useReportLocalTimezone());
    expect(reportLocalTimezone).not.toHaveBeenCalled();

    identity.ready = true;
    identity.viewer = "alice";
    rerender();
    expect(reportLocalTimezone).toHaveBeenCalledTimes(1);
  });

  it("reports for a server without auth, where the viewer id stays null", () => {
    identity.ready = true;
    renderHook(() => useReportLocalTimezone());
    expect(reportLocalTimezone).toHaveBeenCalledTimes(1);
  });

  it("reports again when the viewer changes", () => {
    identity.ready = true;
    identity.viewer = "alice";
    const { rerender } = renderHook(() => useReportLocalTimezone());
    identity.viewer = "bob";
    rerender();
    expect(reportLocalTimezone).toHaveBeenCalledTimes(2);
  });
});

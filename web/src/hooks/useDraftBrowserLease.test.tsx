import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renewDraftBrowserLease } from "@/lib/nativeBridge";
import { DRAFT_BROWSER_HEARTBEAT_INTERVAL_MS, useDraftBrowserLease } from "./useDraftBrowserLease";

vi.mock("@/lib/nativeBridge", () => ({
  renewDraftBrowserLease: vi.fn().mockResolvedValue({ ok: true, renewed: true }),
  supportsBrowser: () => true,
}));

describe("useDraftBrowserLease", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(renewDraftBrowserLease).mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps renewing while New Chat remains mounted with its panel collapsed", () => {
    const { rerender } = renderHook(
      ({ landing }) => useDraftBrowserLease("draft-workspace:1234", landing),
      { initialProps: { landing: true } },
    );
    expect(renewDraftBrowserLease).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(DRAFT_BROWSER_HEARTBEAT_INTERVAL_MS * 3));
    expect(renewDraftBrowserLease).toHaveBeenCalledTimes(4);

    rerender({ landing: false });
    act(() => vi.advanceTimersByTime(DRAFT_BROWSER_HEARTBEAT_INTERVAL_MS * 3));
    expect(renewDraftBrowserLease).toHaveBeenCalledTimes(4);
  });

  it("moves renewal ownership to a new landing namespace", () => {
    const { rerender } = renderHook(({ workspaceId }) => useDraftBrowserLease(workspaceId, true), {
      initialProps: { workspaceId: "draft-workspace:first" },
    });
    rerender({ workspaceId: "draft-workspace:second" });
    act(() => vi.advanceTimersByTime(DRAFT_BROWSER_HEARTBEAT_INTERVAL_MS));

    expect(renewDraftBrowserLease).toHaveBeenNthCalledWith(1, "draft-workspace:first");
    expect(renewDraftBrowserLease).toHaveBeenNthCalledWith(2, "draft-workspace:second");
    expect(renewDraftBrowserLease).toHaveBeenNthCalledWith(3, "draft-workspace:second");
  });
});

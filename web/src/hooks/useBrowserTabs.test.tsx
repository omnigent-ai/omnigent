import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { emitBrowserActionRequest } from "@/lib/browserActionBus";
import { readSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { browserViewId, useBrowserTabs } from "./useBrowserTabs";

afterEach(() => {
  cleanup();
  localStorage.clear();
  Reflect.deleteProperty(window, "omnigentDesktop");
});

describe("browser soft tabs", () => {
  it("persists a pending close even if the workspace unmounts", async () => {
    let finishClose!: (value: { ok: boolean }) => void;
    const browserClose = vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          finishClose = resolve;
        }),
    );
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result, unmount } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const pendingClose = result.current.close(result.current.selected!);
    unmount();
    await act(async () => {
      finishClose({ ok: true });
      await pendingClose;
    });
    expect(readSessionWorkspaceState("session-a").openBrowsers).toEqual([]);
  });

  it("creates independent views and restores the selection on remount", () => {
    const first = renderHook(() => useBrowserTabs("session-a"));
    expect(first.result.current.viewId).toBe("session-a");
    act(() => first.result.current.add());
    const firstId = first.result.current.selected!;
    act(() => first.result.current.add());
    const secondId = first.result.current.selected!;
    expect(secondId).not.toBe(firstId);
    expect(first.result.current.tabs).toEqual([firstId, secondId]);
    expect(browserViewId("session-a", firstId)).not.toBe(browserViewId("session-b", firstId));
    first.unmount();
    const restored = renderHook(() => useBrowserTabs("session-a"));
    expect(restored.result.current.selected).toBe(secondId);
    expect(restored.result.current.tabs).toEqual([firstId, secondId]);
    const other = renderHook(() => useBrowserTabs("session-b"));
    expect(other.result.current.tabs).toEqual([]);
  });

  it("closes only the target view and selects a neighbor, then the default", async () => {
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const firstId = result.current.selected!;
    act(() => result.current.add());
    const secondId = result.current.selected!;
    await act(() => result.current.close(secondId));
    expect(browserClose).toHaveBeenCalledWith(browserViewId("session-a", secondId));
    expect(result.current.selected).toBe(firstId);
    await act(() => result.current.close(firstId));
    expect(result.current.viewId).toBe("session-a");
    expect(readSessionWorkspaceState("session-a").openBrowsers).toEqual([]);
  });

  it("keeps the selection when closing a background tab or a close fails", async () => {
    const browserClose = vi.fn().mockResolvedValue({ ok: true });
    Object.assign(window, { omnigentDesktop: { browserClose } });
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const firstId = result.current.selected!;
    act(() => result.current.add());
    const secondId = result.current.selected!;
    await act(() => result.current.close(firstId));
    expect(result.current.selected).toBe(secondId);
    browserClose.mockRejectedValueOnce(new Error("disconnected"));
    await act(() => result.current.close(secondId));
    expect(result.current.tabs).toEqual([secondId]);
  });

  it("surfaces only the owning session's default browser for agent navigation", () => {
    const { result } = renderHook(() => useBrowserTabs("session-a"));
    act(() => result.current.add());
    const selected = result.current.selected;
    const event = {
      type: "browser_action_request" as const,
      actionId: "navigate-1",
      action: "navigate",
      args: {},
    };
    act(() => emitBrowserActionRequest(event, "session-b"));
    expect(result.current.selected).toBe(selected);
    act(() => emitBrowserActionRequest(event, "session-a"));
    expect(result.current.selected).toBeNull();
    expect(result.current.tabs).toEqual([selected]);
  });
});

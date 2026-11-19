import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";

import { useFloatingComposerSupport } from "./useFloatingComposerSupport";

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));

const announceSupport = vi.fn();
let getDesktopFeatures: ReturnType<typeof vi.fn>;

function stubShell(shell: Record<string, unknown> | undefined): void {
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = shell;
}

function flag(on: boolean): void {
  vi.mocked(useServerInfo).mockReturnValue({
    ...FALLBACK_SERVER_INFO,
    features: { floating_composer: on },
  });
}

beforeEach(() => {
  announceSupport.mockReset();
  getDesktopFeatures = vi.fn().mockResolvedValue({ floatingComposer: true });
  stubShell({
    kind: "electron",
    getDesktopFeatures,
    floatingComposer: {
      announceSupport,
      dismiss: vi.fn(),
      openInMainWindow: vi.fn(),
      filePicker: vi.fn(),
    },
  });
  flag(true);
});

afterEach(() => {
  cleanup();
  stubShell(undefined);
});

describe("useFloatingComposerSupport", () => {
  it("announces version 1 once with the flag on in a shell that can show the panel", async () => {
    const { rerender } = renderHook(() => useFloatingComposerSupport());
    await waitFor(() => expect(announceSupport).toHaveBeenCalledWith(1));
    rerender();
    await Promise.resolve();
    expect(announceSupport).toHaveBeenCalledTimes(1);
  });

  it("announces nothing while the flag is off", async () => {
    flag(false);
    renderHook(() => useFloatingComposerSupport());
    await Promise.resolve();
    expect(getDesktopFeatures).not.toHaveBeenCalled();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing while server info is loading", async () => {
    vi.mocked(useServerInfo).mockReturnValue("loading");
    renderHook(() => useFloatingComposerSupport());
    await Promise.resolve();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing outside the desktop shell, or in a shell without the panel bridge", async () => {
    stubShell(undefined);
    renderHook(() => useFloatingComposerSupport());
    stubShell({ kind: "electron", getDesktopFeatures });
    renderHook(() => useFloatingComposerSupport());
    await Promise.resolve();
    expect(getDesktopFeatures).not.toHaveBeenCalled();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it.each([
    ["doesn't report the panel", { floatingComposer: false }],
    ["has no desktop features", null],
  ])("announces nothing when the shell %s", async (_case, features) => {
    getDesktopFeatures.mockResolvedValue(features);
    renderHook(() => useFloatingComposerSupport());
    await waitFor(() => expect(getDesktopFeatures).toHaveBeenCalled());
    await Promise.resolve();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing when asking the shell for its features fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    getDesktopFeatures.mockRejectedValue(new Error("shell gone"));
    renderHook(() => useFloatingComposerSupport());
    await waitFor(() => expect(getDesktopFeatures).toHaveBeenCalled());
    await Promise.resolve();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("doesn't announce for a page that unmounted before the shell answered", async () => {
    let answer: (features: { floatingComposer: boolean }) => void = () => {};
    getDesktopFeatures.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const { unmount } = renderHook(() => useFloatingComposerSupport());
    unmount();
    answer({ floatingComposer: true });
    await Promise.resolve();
    await Promise.resolve();
    expect(announceSupport).not.toHaveBeenCalled();
  });
});

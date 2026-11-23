import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";

import { useFloatingComposerSupport } from "./useFloatingComposerSupport";

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));

const announceSupport = vi.fn();
const navigate = vi.fn();
const takeLaunch = vi.fn();
const confirmLaunch = vi.fn();
const unsubscribeOffers = vi.fn();
const onLaunchOffer = vi.fn(() => unsubscribeOffers);
let getDesktopFeatures: ReturnType<typeof vi.fn>;

function stubShell(shell: Record<string, unknown> | undefined): void {
  (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop = shell;
}

/** Lets every pending promise callback run, however many hops the shell's answer takes. */
function settle(): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

function flag(on: boolean): void {
  vi.mocked(useServerInfo).mockReturnValue({
    ...FALLBACK_SERVER_INFO,
    features: { floating_composer: on },
  });
}

beforeEach(() => {
  announceSupport.mockReset();
  navigate.mockReset();
  takeLaunch.mockReset();
  confirmLaunch.mockReset().mockResolvedValue(true);
  onLaunchOffer.mockClear();
  unsubscribeOffers.mockReset();
  getDesktopFeatures = vi.fn().mockResolvedValue({ floatingComposer: true });
  stubShell({
    kind: "electron",
    getDesktopFeatures,
    floatingComposer: {
      announceSupport,
      dismiss: vi.fn(),
      openInMainWindow: vi.fn(),
      filePicker: vi.fn(),
      onLaunchOffer,
      takeLaunch,
      confirmLaunch,
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
    // StrictMode mounts the effect twice, as the app does in development.
    const { rerender } = renderHook(() => useFloatingComposerSupport(navigate), {
      reactStrictMode: true,
    });
    await waitFor(() => expect(announceSupport).toHaveBeenCalledWith(1));
    rerender();
    await settle();
    expect(getDesktopFeatures).toHaveBeenCalledTimes(2);
    expect(announceSupport).toHaveBeenCalledTimes(1);
  });

  it("announces nothing while the flag is off", async () => {
    flag(false);
    renderHook(() => useFloatingComposerSupport(navigate));
    await settle();
    expect(getDesktopFeatures).not.toHaveBeenCalled();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing while server info is loading", async () => {
    vi.mocked(useServerInfo).mockReturnValue("loading");
    renderHook(() => useFloatingComposerSupport(navigate));
    await settle();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing outside the desktop shell, or in a shell without the panel bridge", async () => {
    stubShell(undefined);
    renderHook(() => useFloatingComposerSupport(navigate));
    stubShell({ kind: "electron", getDesktopFeatures });
    renderHook(() => useFloatingComposerSupport(navigate));
    await settle();
    expect(getDesktopFeatures).not.toHaveBeenCalled();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it.each([
    ["doesn't report the panel", { floatingComposer: false }],
    ["has no desktop features", null],
  ])("announces nothing when the shell %s", async (_case, features) => {
    getDesktopFeatures.mockResolvedValue(features);
    renderHook(() => useFloatingComposerSupport(navigate));
    await waitFor(() => expect(getDesktopFeatures).toHaveBeenCalled());
    await settle();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing when asking the shell for its features fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    getDesktopFeatures.mockRejectedValue(new Error("shell gone"));
    renderHook(() => useFloatingComposerSupport(navigate));
    await waitFor(() => expect(getDesktopFeatures).toHaveBeenCalled());
    await settle();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("doesn't announce for a page that unmounted before the shell answered", async () => {
    let answer: (features: { floatingComposer: boolean }) => void = () => {};
    getDesktopFeatures.mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    const { unmount } = renderHook(() => useFloatingComposerSupport(navigate));
    unmount();
    answer({ floatingComposer: true });
    await settle();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("logs instead of rejecting when the shell's announce call throws", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    announceSupport.mockImplementation(() => {
      throw new Error("preload gone");
    });
    renderHook(() => useFloatingComposerSupport(navigate));
    await waitFor(() =>
      expect(warn).toHaveBeenCalledWith(
        "[floatingComposer] couldn't announce support or subscribe to launches:",
        expect.any(Error),
      ),
    );
  });
});

describe("useFloatingComposerSupport launch offers", () => {
  const offer = { id: "offer-1", sessionId: "conv_1", path: "/c/conv_1" };

  it("subscribes after announcing, and opens the chat only for a launch the page takes", async () => {
    renderHook(() => useFloatingComposerSupport(navigate));
    await settle();
    expect(announceSupport).toHaveBeenCalledTimes(1);
    expect(onLaunchOffer).toHaveBeenCalledTimes(1);
    // The shell hears the announcement first, and only then that this page takes launches.
    expect(announceSupport.mock.invocationCallOrder[0]).toBeLessThan(
      onLaunchOffer.mock.invocationCallOrder[0]!,
    );
    const [heard] = onLaunchOffer.mock.calls[0] as unknown as [(offer: unknown) => void];
    takeLaunch.mockResolvedValueOnce(null);
    heard(offer);
    await settle();
    expect(takeLaunch).toHaveBeenCalledWith("offer-1");
    expect(confirmLaunch).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    takeLaunch.mockResolvedValueOnce({ text: "Fix the build", skill: null, files: [] });
    heard(offer);
    await settle();
    expect(confirmLaunch).toHaveBeenCalledWith("offer-1");
    expect(navigate).toHaveBeenCalledWith("/c/conv_1");
  });

  it("stops taking offers when its page unmounts", async () => {
    const { unmount } = renderHook(() => useFloatingComposerSupport(navigate));
    await settle();
    unmount();
    expect(unsubscribeOffers).toHaveBeenCalledTimes(1);
  });

  it("still announces in a shell that predates launch offers, without a warning", async () => {
    // The hook's catch would swallow a TypeError here, so the warning is what shows a regression.
    // An earlier test may have left its console.warn spy in place, so start from a clean count.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    warn.mockClear();
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
    renderHook(() => useFloatingComposerSupport(navigate));
    await settle();
    expect(announceSupport).toHaveBeenCalledTimes(1);
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });
});

import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import type { FloatingComposerFirstMessage } from "@/lib/nativeBridge";
import { setPendingInitialPrompt } from "@/store/chatStore";

import { useFloatingComposerSupport } from "./useFloatingComposerSupport";

const navigate = vi.hoisted(() => vi.fn());
vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("@/store/chatStore", () => ({ setPendingInitialPrompt: vi.fn() }));

interface Launch {
  path: string;
  firstMessage: FloatingComposerFirstMessage;
}
const announceSupport = vi.fn();
const onLaunch = vi.fn();
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
  onLaunch.mockReset();
  navigate.mockReset();
  vi.mocked(setPendingInitialPrompt).mockReset();
  getDesktopFeatures = vi.fn().mockResolvedValue({ floatingComposer: true });
  stubShell({
    kind: "electron",
    getDesktopFeatures,
    floatingComposer: {
      announceSupport,
      dismiss: vi.fn(),
      openInMainWindow: vi.fn(),
      filePicker: vi.fn(),
      onLaunch,
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
    const { rerender } = renderHook(() => useFloatingComposerSupport(), { reactStrictMode: true });
    await waitFor(() => expect(announceSupport).toHaveBeenCalledWith(1));
    rerender();
    await settle();
    expect(getDesktopFeatures).toHaveBeenCalledTimes(2);
    expect(announceSupport).toHaveBeenCalledTimes(1);
  });

  it("announces nothing while the flag is off", async () => {
    flag(false);
    renderHook(() => useFloatingComposerSupport());
    await settle();
    expect(getDesktopFeatures).not.toHaveBeenCalled();
    expect(announceSupport).not.toHaveBeenCalled();
    expect(onLaunch).not.toHaveBeenCalled();
  });

  it("opens a session the panel launched with its first message, as New Chat does", async () => {
    let launch: (launch: Launch) => void = () => {};
    const stopListening = vi.fn();
    onLaunch.mockImplementation((callback: (launch: Launch) => void) => {
      launch = callback;
      return stopListening;
    });
    const { unmount } = renderHook(() => useFloatingComposerSupport());
    await waitFor(() => expect(onLaunch).toHaveBeenCalledTimes(1));

    const firstMessage: FloatingComposerFirstMessage = {
      text: "Fix the build",
      skill: null,
      files: [
        {
          name: "notes.txt",
          type: "text/plain",
          lastModified: 7,
          data: new TextEncoder().encode("x"),
        },
      ],
    };
    // Only a session's route takes a first message.
    launch({ path: "/settings/general", firstMessage });
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();

    launch({ path: "/c/conv_1", firstMessage });
    const [sessionId, prompt] = vi.mocked(setPendingInitialPrompt).mock.calls[0]!;
    expect(sessionId).toBe("conv_1");
    expect(prompt).toMatchObject({ text: "Fix the build", skill: null });
    const [file] = prompt.files!;
    expect([file!.name, file!.type, file!.lastModified]).toEqual(["notes.txt", "text/plain", 7]);
    expect(await file!.text()).toBe("x");
    // The chat finds the message waiting when it opens.
    expect(navigate).toHaveBeenCalledWith("/c/conv_1");
    expect(vi.mocked(setPendingInitialPrompt).mock.invocationCallOrder[0]).toBeLessThan(
      navigate.mock.invocationCallOrder[0]!,
    );

    unmount();
    expect(stopListening).toHaveBeenCalledTimes(1);
  });

  it("announces nothing while server info is loading", async () => {
    vi.mocked(useServerInfo).mockReturnValue("loading");
    renderHook(() => useFloatingComposerSupport());
    await settle();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing outside the desktop shell, or in a shell without the panel bridge", async () => {
    stubShell(undefined);
    renderHook(() => useFloatingComposerSupport());
    stubShell({ kind: "electron", getDesktopFeatures });
    renderHook(() => useFloatingComposerSupport());
    await settle();
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
    await settle();
    expect(announceSupport).not.toHaveBeenCalled();
  });

  it("announces nothing when asking the shell for its features fails", async () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    getDesktopFeatures.mockRejectedValue(new Error("shell gone"));
    renderHook(() => useFloatingComposerSupport());
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
    const { unmount } = renderHook(() => useFloatingComposerSupport());
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
    renderHook(() => useFloatingComposerSupport());
    await waitFor(() =>
      expect(warn).toHaveBeenCalledWith(
        "[floatingComposer] couldn't announce support:",
        expect.any(Error),
      ),
    );
  });
});

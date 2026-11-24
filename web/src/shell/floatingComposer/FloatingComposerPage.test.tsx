import { act, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({
  openInMainWindow: vi.fn(),
  dismiss: vi.fn(),
  filePicker: vi.fn(),
  announceSupport: vi.fn(),
  ready: vi.fn(),
  // Like the shell's, it returns an unsubscribe.
  onShown: vi.fn((_listener: () => void) => () => {}),
}));
const landing = vi.hoisted(() => ({ shown: 0 }));
vi.mock("@/lib/nativeBridge", () => ({ floatingComposerBridge: () => bridge }));
vi.mock("./floatingLaunch", () => ({ launchFromPanel: vi.fn() }));
vi.mock("@/lib/branding", () => ({ useLogoUrl: () => null }));
// New Chat's own behavior has its own suites; here it only types and launches.
vi.mock("@/shell/NewChatDialog", async () => {
  const { useComposerPresentation } = await import("@/shell/composerPresentation");
  return {
    NewChatLandingScreen: function FakeLanding() {
      const presentation = useComposerPresentation();
      const [text, setText] = useState("");
      useEffect(
        () =>
          presentation.kind === "floating"
            ? presentation.subscribeShown(() => (landing.shown += 1))
            : undefined,
        [presentation],
      );
      return (
        <>
          <textarea
            data-testid="new-chat-landing-input"
            value={text}
            onChange={(event) => setText(event.target.value)}
          />
          <button
            type="button"
            onClick={() =>
              presentation.kind === "floating" &&
              presentation.launch({
                sessionId: "conv_1",
                text,
                skill: null,
                inputAccepted: false,
              })
            }
          >
            Launch
          </button>
          <button
            type="button"
            onClick={() =>
              presentation.kind === "floating" && presentation.openAppPage("/settings/integrations")
            }
          >
            Integrations
          </button>
        </>
      );
    },
  };
});

import { FloatingComposerPage, OPEN_PAGE_ERROR } from "./FloatingComposerPage";
import { launchFromPanel } from "./floatingLaunch";

const prompt = () => screen.getByTestId("new-chat-landing-input") as HTMLTextAreaElement;

async function launch(outcome: { error: string | null; freshComposer: boolean }) {
  vi.mocked(launchFromPanel).mockResolvedValue(outcome);
  fireEvent.change(prompt(), { target: { value: "Fix the build" } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Launch" }));
  });
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("FloatingComposerPage", () => {
  it("tells a hidden panel's shell it's ready at once, since a hidden window gets no frames", async () => {
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    try {
      vi.resetModules();
      const { FloatingComposerPage: FreshPage } = await import("./FloatingComposerPage");
      render(<FreshPage />);
      expect(bridge.ready).toHaveBeenCalledTimes(1);
    } finally {
      visibility.mockRestore();
    }
  });

  it("tells the shell it's ready once per document, after the composer painted", async () => {
    const frames: FrameRequestCallback[] = [];
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frames.push(callback);
      return frames.length;
    });
    try {
      // A fresh module is a fresh document.
      vi.resetModules();
      const { FloatingComposerPage: FreshPage } = await import("./FloatingComposerPage");
      const first = render(<FreshPage />);
      expect(bridge.ready).not.toHaveBeenCalled();
      // The first frame's callback runs before that frame paints; ready waits for the next one.
      for (const frame of frames.splice(0)) frame(0);
      expect(bridge.ready).not.toHaveBeenCalled();
      for (const frame of frames.splice(0)) frame(0);
      expect(bridge.ready).toHaveBeenCalledTimes(1);
      first.unmount();
      render(<FreshPage />);
      for (const frame of frames.splice(0)) frame(0);
      for (const frame of frames.splice(0)) frame(0);
      expect(bridge.ready).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("works with a shell too old to hear that the panel is ready", async () => {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      callback(0);
      return 0;
    });
    const ready = bridge.ready;
    try {
      // Older shells show the panel without a readiness signal.
      Reflect.deleteProperty(bridge, "ready");
      vi.resetModules();
      const { FloatingComposerPage: FreshPage } = await import("./FloatingComposerPage");
      expect(() => render(<FreshPage />)).not.toThrow();
      expect(prompt()).toBeInTheDocument();
    } finally {
      Object.assign(bridge, { ready });
      vi.unstubAllGlobals();
    }
  });

  it("hands the shell's word that the panel took the keyboard to the composer", () => {
    landing.shown = 0;
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    const stopListening = vi.fn();
    vi.mocked(bridge.onShown).mockReturnValueOnce(stopListening);
    try {
      const page = render(<FloatingComposerPage />);
      expect(landing.shown).toBe(0);
      const shown = vi.mocked(bridge.onShown).mock.calls.at(-1)![0];
      act(() => shown());
      expect(landing.shown).toBe(1);
      // With the shell saying so, a window focus alone doesn't move the keyboard.
      act(() => window.dispatchEvent(new Event("focus")));
      expect(landing.shown).toBe(1);
      page.unmount();
      expect(stopListening).toHaveBeenCalledTimes(1);
    } finally {
      visibility.mockRestore();
    }
  });

  it("gives the composer the keyboard on its first render when the panel is already up", () => {
    landing.shown = 0;
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    try {
      render(<FloatingComposerPage />);
      expect(landing.shown).toBe(1);
    } finally {
      visibility.mockRestore();
    }
  });

  it("with an older shell, stands in only when the window takes focus with nothing focused", () => {
    landing.shown = 0;
    const onShown = bridge.onShown;
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    try {
      Reflect.deleteProperty(bridge, "onShown");
      render(<FloatingComposerPage />);
      act(() => {
        (document.activeElement as HTMLElement | null)?.blur();
        window.dispatchEvent(new Event("focus"));
      });
      expect(landing.shown).toBe(1);
      // Something in the panel has the keyboard: a window focus doesn't take it away.
      act(() => {
        screen.getByRole("button", { name: "Launch" }).focus();
        window.dispatchEvent(new Event("focus"));
      });
      expect(landing.shown).toBe(1);
    } finally {
      Object.assign(bridge, { onShown });
      visibility.mockRestore();
    }
  });

  it("hides on Escape and keeps what was typed", () => {
    render(<FloatingComposerPage />);
    fireEvent.change(prompt(), { target: { value: "half a thought" } });

    fireEvent.keyDown(window, { key: "Escape" });

    expect(bridge.dismiss).toHaveBeenCalledTimes(1);
    expect(prompt().value).toBe("half a thought");
  });

  it("leaves Escape to an open menu", () => {
    render(
      <>
        <FloatingComposerPage />
        <div role="menu" data-state="open" />
      </>,
    );

    fireEvent.keyDown(window, { key: "Escape" });

    expect(bridge.dismiss).not.toHaveBeenCalled();
  });

  it("hides from the header's Close", () => {
    render(<FloatingComposerPage />);

    fireEvent.click(screen.getByRole("button", { name: "Close New Agent" }));

    expect(bridge.dismiss).toHaveBeenCalledTimes(1);
  });

  it("starts a fresh composer after a delivered launch", async () => {
    render(<FloatingComposerPage />);

    await launch({ error: null, freshComposer: true });

    expect(launchFromPanel).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "conv_1", text: "Fix the build" }),
    );
    expect(prompt().value).toBe("");
    expect(screen.queryByTestId("floating-launch-error")).toBeNull();
  });

  it("shows why a launch failed and keeps the prompt", async () => {
    render(<FloatingComposerPage />);

    await launch({ error: "Couldn't send it.", freshComposer: false });

    expect(screen.getByTestId("floating-launch-error")).toHaveTextContent("Couldn't send it.");
    expect(prompt().value).toBe("Fix the build");
  });

  it("starts a fresh composer and still says why the session couldn't be shown", async () => {
    render(<FloatingComposerPage />);

    await launch({ error: "Couldn't show it.", freshComposer: true });

    expect(prompt().value).toBe("");
    expect(screen.getByTestId("floating-launch-error")).toHaveTextContent("Couldn't show it.");
  });

  it.each([
    [
      "refuses",
      () => bridge.openInMainWindow.mockResolvedValue({ ok: false, error: "window-loading" }),
    ],
    ["fails", () => bridge.openInMainWindow.mockRejectedValue(new Error("gone"))],
  ])("says so when the shell %s to open a linked page", async (_case, arrange) => {
    arrange();
    render(<FloatingComposerPage />);

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Integrations" }));
    });

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/settings/integrations");
    expect(screen.getByTestId("floating-launch-error")).toHaveTextContent(OPEN_PAGE_ERROR);
  });

  it("ignores an older launch that settles after a newer one", async () => {
    let settleFirst: (outcome: { error: string | null; freshComposer: boolean }) => void = () => {};
    vi.mocked(launchFromPanel)
      .mockReturnValueOnce(
        new Promise((resolve) => {
          settleFirst = resolve;
        }),
      )
      .mockResolvedValueOnce({ error: "Couldn't send it.", freshComposer: false });
    render(<FloatingComposerPage />);
    fireEvent.change(prompt(), { target: { value: "Fix the build" } });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Launch" }));
      fireEvent.click(screen.getByRole("button", { name: "Launch" }));
    });

    await act(async () => settleFirst({ error: null, freshComposer: true }));

    expect(screen.getByTestId("floating-launch-error")).toHaveTextContent("Couldn't send it.");
    expect(prompt().value).toBe("Fix the build");
  });
});

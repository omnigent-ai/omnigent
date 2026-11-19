import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const bridge = vi.hoisted(() => ({
  openInMainWindow: vi.fn(),
  dismiss: vi.fn(),
  filePicker: vi.fn(),
  announceSupport: vi.fn(),
}));
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

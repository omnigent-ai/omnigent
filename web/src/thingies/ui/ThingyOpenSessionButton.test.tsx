import { useState } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  requestThingyCreateSessionMenu,
  resetThingyRailRequests,
  useThingyRailRequest,
} from "@/lib/thingyRailTabs";
import {
  MobileHeaderControlPortal,
  MobileHeaderControlsProvider,
  SessionControlPortal,
} from "@/shell/MobileHeaderControlsSlot";
import { ThingyOpenSessionButton } from "./ThingyOpenSessionButton";

// A composer: its host, permission and harness controls keep their own state, like New
// session's draft, and render through the session control portals the real composers use.
function Composer({
  sessionId,
  harness,
  createError,
}: {
  sessionId: string | null;
  harness: string;
  createError?: string;
}) {
  const [mode, setMode] = useState("Ask");
  const [created, setCreated] = useState(false);
  return (
    <div data-testid="composer">
      <MobileHeaderControlPortal sessionId={sessionId} popoverSlot="host">
        <span>Host: laptop</span>
      </MobileHeaderControlPortal>
      <SessionControlPortal sessionId={sessionId} slot="permission">
        <button type="button" onClick={() => setMode("Auto")}>
          Permission: {mode}
        </button>
      </SessionControlPortal>
      <MobileHeaderControlPortal sessionId={sessionId} popoverSlot="harness">
        <span>Model: {harness}-model</span>
      </MobileHeaderControlPortal>
      <SessionControlPortal sessionId={sessionId} slot="icon" inPlace={false}>
        <svg data-testid={`${harness}-icon`} />
      </SessionControlPortal>
      {/* New session's Create session button, with its error, as the landing renders them. */}
      <SessionControlPortal sessionId={sessionId} slot="create" inPlace={false}>
        {createError && <p role="alert">{createError}</p>}
        <button type="button" onClick={() => setCreated(true)}>
          Create session
        </button>
      </SessionControlPortal>
      {created && <span>Session created as {mode}</span>}
    </div>
  );
}

function renderButton({
  sessionId,
  composerSessionId = sessionId,
  harness = "codex",
  onOpenSession = vi.fn(),
  createError,
}: {
  sessionId: string | null;
  composerSessionId?: string | null;
  harness?: string;
  onOpenSession?: () => void;
  createError?: string;
}) {
  render(
    <MobileHeaderControlsProvider>
      <ThingyOpenSessionButton sessionId={sessionId} onOpenSession={onOpenSession} />
      <Composer sessionId={composerSessionId} harness={harness} createError={createError} />
    </MobileHeaderControlsProvider>,
  );
  return { onOpenSession, button: screen.getByRole("button", { name: "Open session" }) };
}

function hoverOpen(button: HTMLElement) {
  fireEvent.pointerOver(button);
  act(() => {
    vi.advanceTimersByTime(400);
  });
}

describe("ThingyOpenSessionButton", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    resetThingyRailRequests();
  });

  it("shows the session's harness icon in color", () => {
    renderButton({ sessionId: "conv_1", harness: "claude" });

    const icon = screen.getByTestId("thingy-open-session-icon");
    expect(within(icon).getByTestId("claude-icon")).toBeInTheDocument();
    expect(icon).not.toHaveClass("grayscale");
  });

  it("shows New session's harness in grey before the session exists", () => {
    renderButton({ sessionId: null, harness: "codex" });

    const icon = screen.getByTestId("thingy-open-session-icon");
    expect(within(icon).getByTestId("codex-icon")).toBeInTheDocument();
    expect(icon).toHaveClass("grayscale");
  });

  it("keeps another session's controls out of the button", () => {
    renderButton({ sessionId: "conv_1", composerSessionId: "conv_2" });

    const icon = screen.getByTestId("thingy-open-session-icon");
    expect(icon).toBeEmptyDOMElement();
    expect(screen.getByTestId("composer")).toHaveTextContent("Permission: Ask");
  });

  it("opens the composer's controls in a popover on hover", () => {
    const { button } = renderButton({ sessionId: "conv_1" });
    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();

    hoverOpen(button);

    const popover = screen.getByTestId("thingy-open-session-popover");
    expect(within(popover).getByText("Host: laptop")).toBeInTheDocument();
    expect(within(popover).getByRole("button", { name: "Permission: Ask" })).toBeInTheDocument();
    expect(within(popover).getByText("Model: codex-model")).toBeInTheDocument();
    // The controls moved; the composer no longer shows them.
    expect(screen.getByTestId("composer")).not.toHaveTextContent("Host: laptop");
  });

  it("doesn't open for a pointer that only passes over the button", () => {
    const { button } = renderButton({ sessionId: "conv_1" });

    fireEvent.pointerOver(button);
    act(() => {
      vi.advanceTimersByTime(100);
    });
    fireEvent.pointerLeave(button);
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
  });

  it("applies a choice made in the popover to the composer's own draft", () => {
    const { button } = renderButton({ sessionId: null });
    hoverOpen(button);

    fireEvent.click(screen.getByRole("button", { name: "Permission: Ask" }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
    expect(screen.getByTestId("composer")).toHaveTextContent("Permission: Auto");
  });

  it("stays open while its controls are in use, even when the pointer leaves", () => {
    const { button } = renderButton({ sessionId: "conv_1" });
    hoverOpen(button);
    const popover = screen.getByTestId("thingy-open-session-popover");

    fireEvent.pointerDown(within(popover).getByRole("button", { name: "Permission: Ask" }));
    fireEvent.pointerLeave(popover);
    act(() => {
      vi.advanceTimersByTime(1000);
    });

    expect(screen.getByTestId("thingy-open-session-popover")).toBeInTheDocument();
  });

  it("leaves full screen when the icon is clicked", () => {
    const { button, onOpenSession } = renderButton({ sessionId: "conv_1" });

    fireEvent.pointerDown(button);
    fireEvent.focus(button);
    fireEvent.click(button);

    expect(onOpenSession).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
  });

  it("leaves full screen from the popover's Open session button", () => {
    const { button, onOpenSession } = renderButton({ sessionId: "conv_1" });
    hoverOpen(button);

    fireEvent.click(screen.getByTestId("thingy-open-session-button"));

    expect(onOpenSession).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
    expect(screen.getByTestId("composer")).toHaveTextContent("Host: laptop");
  });

  it("opens on keyboard focus and closes on Escape", () => {
    const { button } = renderButton({ sessionId: "conv_1" });

    fireEvent.focus(button);
    expect(screen.getByTestId("thingy-open-session-popover")).toBeInTheDocument();

    fireEvent.keyDown(button, { key: "Escape" });
    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
  });

  it("offers New session's Create session instead of Open session before the session exists", () => {
    const { button } = renderButton({ sessionId: null });
    hoverOpen(button);

    const popover = screen.getByRole("dialog", { name: "Create session" });
    expect(within(popover).getByRole("button", { name: "Create session" })).toBeInTheDocument();
    expect(within(popover).getByTestId("thingy-open-session-create")).not.toBeEmptyDOMElement();
  });

  it("opens the Create session menu, focused, when the thingy asks before its session exists", () => {
    renderButton({ sessionId: null });

    act(() => {
      requestThingyCreateSessionMenu();
    });
    act(() => {
      vi.advanceTimersByTime(50);
    });

    const popover = screen.getByRole("dialog", { name: "Create session" });
    expect(document.activeElement).toBe(
      within(popover).getByRole("button", { name: "Create session" }),
    );
    // The request is taken, so the menu doesn't open again on its own.
    expect(useThingyRailRequest.getState().createSessionMenuRequested).toBe(false);

    // Opened for a request, it stays while the pointer moves away, and Escape closes it.
    fireEvent.pointerLeave(popover);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getByTestId("thingy-open-session-popover")).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
  });

  it("creates the session with the choices made in the menu, without leaving full screen", () => {
    const { onOpenSession } = renderButton({ sessionId: null });
    act(() => {
      requestThingyCreateSessionMenu();
    });

    fireEvent.click(screen.getByRole("button", { name: "Permission: Ask" }));
    fireEvent.click(screen.getByRole("button", { name: "Create session" }));

    expect(screen.getByText("Session created as Auto")).toBeInTheDocument();
    expect(onOpenSession).not.toHaveBeenCalled();
  });

  it("shows why creating the session failed in the menu", () => {
    renderButton({ sessionId: null, createError: "No host is connected" });
    act(() => {
      requestThingyCreateSessionMenu();
    });

    expect(
      within(screen.getByTestId("thingy-open-session-popover")).getByRole("alert"),
    ).toHaveTextContent("No host is connected");
  });

  it("leaves a request for the Create session menu to New session's button", () => {
    renderButton({ sessionId: "conv_1" });

    act(() => {
      requestThingyCreateSessionMenu();
    });

    expect(screen.queryByTestId("thingy-open-session-popover")).toBeNull();
    expect(useThingyRailRequest.getState().createSessionMenuRequested).toBe(true);
  });

  it("hides rows for controls the composer doesn't offer", () => {
    render(
      <MobileHeaderControlsProvider>
        <ThingyOpenSessionButton sessionId="conv_1" onOpenSession={() => undefined} />
        <MobileHeaderControlPortal sessionId="conv_1" popoverSlot="harness">
          <span>Model: only</span>
        </MobileHeaderControlPortal>
      </MobileHeaderControlsProvider>,
    );
    hoverOpen(screen.getByRole("button", { name: "Open session" }));

    expect(screen.getByTestId("thingy-open-session-host")).toBeEmptyDOMElement();
    expect(screen.getByTestId("thingy-open-session-harness")).toHaveTextContent("Model: only");
    // No icon was offered, so the button shows the chat icon.
    expect(screen.getByTestId("thingy-open-session-icon")).toBeEmptyDOMElement();
  });
});

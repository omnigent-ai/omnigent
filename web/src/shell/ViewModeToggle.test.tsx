import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ViewModeToggle } from "./ViewModeToggle";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "./TerminalFirstContext";

// The header toggle is suppressed in the iOS shell (the switcher is the native
// Liquid Glass bar there); default the mock to "not iOS" for the web cases.
const isIOSShellMock = vi.fn(() => false);
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => isIOSShellMock(),
}));

function makeCtx(overrides: Partial<TerminalFirstContextValue> = {}): TerminalFirstContextValue {
  return {
    isClaudeNative: true,
    isNativeWrapper: true,
    isTerminalFirst: true,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: vi.fn(),
    terminalsAvailable: true,
    terminalStartingUp: false,
    ...overrides,
  };
}

function renderToggle(ctx: TerminalFirstContextValue | null) {
  return render(
    <TooltipProvider>
      {ctx ? (
        <TerminalFirstContextProvider value={ctx}>
          <ViewModeToggle />
        </TerminalFirstContextProvider>
      ) : (
        <ViewModeToggle />
      )}
    </TooltipProvider>,
  );
}

/** The Chat segment — icon-only, so it's addressed by its accessible name. */
function chatSegment() {
  return screen.getByRole("button", { name: /^chat view$/i });
}

/** The Terminal segment. Its name doubles as the starting-up explanation. */
function terminalSegment() {
  return screen.getByRole("button", { name: /^terminal (view|is starting up…)$/i });
}

beforeEach(() => {
  isIOSShellMock.mockReturnValue(false);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ViewModeToggle", () => {
  it("renders both segments for terminal-first sessions", () => {
    renderToggle(makeCtx());
    expect(screen.getByRole("group", { name: /switch between chat and terminal/i })).toBeVisible();
    expect(chatSegment()).toBeVisible();
    expect(terminalSegment()).toBeVisible();
  });

  it("renders nothing for a non-terminal-first session", () => {
    const { container } = renderToggle(makeCtx({ isTerminalFirst: false }));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing outside a provider", () => {
    const { container } = renderToggle(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing while a shell owns the main view (isShellView)", () => {
    const { container } = renderToggle(makeCtx({ isShellView: true, view: "terminal" }));
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing in the iOS shell (native bar owns the switcher)", () => {
    isIOSShellMock.mockReturnValue(true);
    const { container } = renderToggle(makeCtx());
    expect(container).toBeEmptyDOMElement();
  });

  it("presses only the active segment in the chat view", () => {
    renderToggle(makeCtx({ view: "chat" }));
    expect(chatSegment()).toHaveAttribute("aria-pressed", "true");
    expect(terminalSegment()).toHaveAttribute("aria-pressed", "false");
  });

  it("presses only the active segment in the terminal view", () => {
    renderToggle(makeCtx({ view: "terminal" }));
    expect(terminalSegment()).toHaveAttribute("aria-pressed", "true");
    expect(chatSegment()).toHaveAttribute("aria-pressed", "false");
  });

  it("switches to the terminal view in one click — no menu to open", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, view: "chat" }));
    fireEvent.click(terminalSegment());
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("switches back to the chat view in one click", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, view: "terminal" }));
    fireEvent.click(chatSegment());
    expect(setView).toHaveBeenCalledWith("chat");
  });

  it("names each segment on hover so the icon-only control is legible", async () => {
    renderToggle(makeCtx({ view: "chat" }));
    // Radix opens on a real pointer move over the trigger (the wrapper span),
    // so a bare pointerEnter on the button wouldn't surface the tooltip.
    fireEvent.pointerMove(chatSegment().parentElement!, { pointerType: "mouse" });
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Chat view");
  });

  it("disables the Terminal segment and shows a spinner while the terminal is coming up", () => {
    renderToggle(makeCtx({ terminalsAvailable: false, terminalStartingUp: true }));
    const terminal = terminalSegment();
    expect(terminal).toBeDisabled();
    // The name carries the reason, so the disabled state explains itself.
    expect(terminal).toHaveAccessibleName(/starting up/i);
    expect(terminal.querySelector(".animate-spin")).not.toBeNull();
  });

  it("disables the Terminal segment WITHOUT a spinner when no terminal exists and none is coming up", () => {
    renderToggle(makeCtx({ terminalsAvailable: false, terminalStartingUp: false }));
    const terminal = terminalSegment();
    expect(terminal).toBeDisabled();
    expect(terminal.querySelector(".animate-spin")).toBeNull();
  });

  it("leaves the Chat segment usable while the terminal is unavailable", () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView, terminalsAvailable: false, view: "terminal" }));
    fireEvent.click(chatSegment());
    expect(setView).toHaveBeenCalledWith("chat");
  });
});

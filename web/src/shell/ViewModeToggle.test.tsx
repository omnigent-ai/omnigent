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

beforeEach(() => {
  isIOSShellMock.mockReturnValue(false);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ViewModeToggle", () => {
  it("renders the MessagesSquare trigger for terminal-first sessions", () => {
    renderToggle(makeCtx());
    expect(
      screen.getByRole("button", { name: /switch between chat and terminal/i }),
    ).toBeInTheDocument();
  });

  it("renders nothing for a non-terminal-first session", () => {
    const { container } = renderToggle(makeCtx({ isTerminalFirst: false }));
    expect(container).toBeEmptyDOMElement();
  });

  it("labels the trigger 'Terminal view' on hover in the terminal view", async () => {
    renderToggle(makeCtx({ view: "terminal" }));
    const trigger = screen.getByRole("button", { name: /switch between chat and terminal/i });
    fireEvent.pointerEnter(trigger);
    // The tooltip content mirrors the active view (portalled, so it appears
    // in addition to the menu item's own label).
    await screen.findByRole("tooltip", { name: /^terminal view$/i });
  });

  it("labels the trigger 'Chat view' on hover in the chat view", async () => {
    renderToggle(makeCtx({ view: "chat" }));
    const trigger = screen.getByRole("button", { name: /switch between chat and terminal/i });
    fireEvent.pointerEnter(trigger);
    await screen.findByRole("tooltip", { name: /^chat view$/i });
  });

  it("suppresses the tooltip while the menu is open", async () => {
    renderToggle(makeCtx({ view: "chat" }));
    const trigger = screen.getByRole("button", { name: /switch between chat and terminal/i });
    // Hover shows the tooltip…
    fireEvent.pointerEnter(trigger);
    await screen.findByRole("tooltip", { name: /^chat view$/i });
    // …but opening the menu must hide it so it doesn't overlap the dropdown.
    fireEvent.pointerDown(trigger, { button: 0 });
    await screen.findByRole("menuitemradio", { name: /^chat$/i });
    expect(screen.queryByRole("tooltip", { name: /^chat view$/i })).toBeNull();
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

  it("marks the current view as checked in the menu", async () => {
    renderToggle(makeCtx({ view: "terminal" }));
    // Radix menus open on pointerdown, not click.
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /switch between chat and terminal/i }),
      { button: 0 },
    );
    const terminalItem = await screen.findByRole("menuitemradio", { name: /^terminal$/i });
    expect(terminalItem).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("menuitemradio", { name: /^chat$/i })).toHaveAttribute(
      "aria-checked",
      "false",
    );
  });

  it("invokes setView when a menu option is selected", async () => {
    const setView = vi.fn();
    renderToggle(makeCtx({ setView }));
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /switch between chat and terminal/i }),
      { button: 0 },
    );
    fireEvent.click(await screen.findByRole("menuitemradio", { name: /^terminal$/i }));
    expect(setView).toHaveBeenCalledWith("terminal");
  });

  it("disables the Terminal option and shows a spinner while the terminal is coming up", async () => {
    renderToggle(makeCtx({ terminalsAvailable: false, terminalStartingUp: true }));
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /switch between chat and terminal/i }),
      { button: 0 },
    );
    const terminalItem = await screen.findByRole("menuitemradio", { name: /^terminal$/i });
    expect(terminalItem).toHaveAttribute("aria-disabled", "true");
    expect(terminalItem.querySelector(".animate-spin")).not.toBeNull();
    expect(terminalItem).toHaveAttribute("title", expect.stringMatching(/starting up/i));
  });

  it("disables the Terminal option WITHOUT a spinner when no terminal exists and none is coming up", async () => {
    renderToggle(makeCtx({ terminalsAvailable: false, terminalStartingUp: false }));
    fireEvent.pointerDown(
      screen.getByRole("button", { name: /switch between chat and terminal/i }),
      { button: 0 },
    );
    const terminalItem = await screen.findByRole("menuitemradio", { name: /^terminal$/i });
    expect(terminalItem).toHaveAttribute("aria-disabled", "true");
    expect(terminalItem.querySelector(".animate-spin")).toBeNull();
  });
});

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ConnectionIndicator } from "./ChatPage";
import type { SessionLiveness } from "@/hooks/useSessionLiveness";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "@/shell/TerminalFirstContext";

/**
 * Build a TerminalFirstContextValue with sensible defaults so each test
 * overrides only the fields it exercises. `terminalStartingUp` defaults to
 * false (the steady state) — spinner tests set it explicitly.
 */
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

function renderWithContext(
  liveness: SessionLiveness,
  ctx: TerminalFirstContextValue | null,
  onShowReconnectHelp = vi.fn(),
) {
  return render(
    <TooltipProvider>
      {ctx ? (
        <TerminalFirstContextProvider value={ctx}>
          <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
        </TerminalFirstContextProvider>
      ) : (
        <ConnectionIndicator liveness={liveness} onShowReconnectHelp={onShowReconnectHelp} />
      )}
    </TooltipProvider>,
  );
}

const ONLINE: SessionLiveness = { kind: "online" };

afterEach(cleanup);

describe("ConnectionIndicator", () => {
  it("renders nothing for an online non-terminal-first session", () => {
    // With status moved to the sidebar, an online non-CN session has
    // nothing to show in the chat band — the composer sits below.
    const { container } = renderWithContext(ONLINE, null);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for an online + non-terminal-first context", () => {
    const { container } = renderWithContext(
      ONLINE,
      makeCtx({ isClaudeNative: false, isTerminalFirst: false, terminalsAvailable: false }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the segmented Chat/Terminal toggle for online terminal-first sessions", () => {
    renderWithContext(ONLINE, makeCtx());
    const group = screen.getByRole("group", { name: /view mode/i });
    expect(group).toBeInTheDocument();
    // Status text has been intentionally removed — the toggle is the
    // entire content now.
    expect(group).not.toHaveTextContent(/agent connected/i);
    expect(screen.getByRole("button", { name: /^chat$/i })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /^terminal$/i })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("renders nothing while a shell owns the main view (isShellView)", () => {
    // A rail-opened shell is chrome-free: a "Chat" option under
    // someone else's shell misreads as the shell being the agent. The
    // shell view's own close X (MainTerminalView) is the way back. A
    // visible pill here means ConnectionIndicator dropped the
    // isShellView gate.
    const { container } = renderWithContext(
      ONLINE,
      makeCtx({ isShellView: true, view: "terminal" }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("disables the Terminal pill and shows a spinner while the terminal is coming up", () => {
    // Coming up: no terminal yet AND the consolidated startingUp flag is
    // set (AppShell folds launch + PTY-creation into it). The greyed button
    // reads as "loading".
    renderWithContext(ONLINE, makeCtx({ terminalsAvailable: false, terminalStartingUp: true }));
    const terminalButton = screen.getByRole("button", { name: /^terminal$/i });
    expect(terminalButton).toBeDisabled();
    // The starting-up state swaps the static terminal glyph for an
    // animated spinner so the greyed-out button reads as "loading".
    expect(terminalButton.querySelector(".animate-spin")).not.toBeNull();
    expect(terminalButton).toHaveAttribute("title", expect.stringMatching(/starting up/i));
  });

  it("disables the Terminal pill WITHOUT a spinner when no terminal exists and none is coming up", () => {
    // Killed-remotely / stopped-idle: the button is greyed out but must
    // NOT spin, since nothing is actually starting up.
    renderWithContext(ONLINE, makeCtx({ terminalsAvailable: false, terminalStartingUp: false }));
    const terminalButton = screen.getByRole("button", { name: /^terminal$/i });
    expect(terminalButton).toBeDisabled();
    expect(terminalButton.querySelector(".animate-spin")).toBeNull();
    expect(terminalButton).not.toHaveAttribute("title");
  });

  it("shows the terminal icon (no spinner) once a terminal is available", () => {
    // Available wins: an openable terminal is never "loading" (AppShell
    // keeps startingUp false whenever terminalsAvailable is true).
    renderWithContext(ONLINE, makeCtx({ terminalsAvailable: true, terminalStartingUp: false }));
    const terminalButton = screen.getByRole("button", { name: /^terminal$/i });
    expect(terminalButton).toBeEnabled();
    expect(terminalButton.querySelector(".animate-spin")).toBeNull();
    expect(terminalButton).not.toHaveAttribute("title");
  });

  it("invokes setView when pill buttons are clicked", () => {
    const setView = vi.fn();
    renderWithContext(ONLINE, makeCtx({ setView }));
    fireEvent.click(screen.getByRole("button", { name: /^terminal$/i }));
    expect(setView).toHaveBeenCalledWith("terminal");

    fireEvent.click(screen.getByRole("button", { name: /^chat$/i }));
    expect(setView).toHaveBeenCalledWith("chat");
  });

  it("renders the reconnect button for a local_stranded session, regardless of context", () => {
    const onShow = vi.fn();
    renderWithContext({ kind: "local_stranded" }, makeCtx(), onShow);
    const button = screen.getByTestId("disconnected-indicator");
    expect(button).toBeInTheDocument();
    fireEvent.click(button);
    expect(onShow).toHaveBeenCalledTimes(1);
    // Switching views while unreachable isn't useful — the terminal
    // panel would be stale anyway — so the toggle is not rendered.
    expect(screen.queryByRole("group", { name: /view mode/i })).toBeNull();
  });

  it("uses host-specific wording for a host_offline session", () => {
    renderWithContext({ kind: "host_offline", isOwner: true }, makeCtx());
    const button = screen.getByTestId("disconnected-indicator");
    expect(button).toHaveTextContent(/host is offline/i);
  });

  it("renders NOTHING for a runner_asleep NON-terminal-first session — composer stays open", () => {
    // Core UX goal: when the host is alive the chat box stays open and
    // the runner relaunches on the next message, so the band shows no
    // disconnect banner. A regular session has no toggle to keep, so the
    // band is empty.
    const { container } = renderWithContext({ kind: "runner_asleep" }, null);
    expect(screen.queryByTestId("disconnected-indicator")).toBeNull();
    expect(container).toBeEmptyDOMElement();
  });

  it("KEEPS the Chat/Terminal pill for a runner_asleep terminal-first session", () => {
    // Stopping a runner must NOT make the toggle vanish: the pill stays so
    // the user can flip views and the next send relaunches the runner
    // (driving the pill's own spinner). Regression for the "pill disappears
    // after stop and never comes back" report.
    renderWithContext({ kind: "runner_asleep" }, makeCtx());
    expect(screen.getByRole("group", { name: /view mode/i })).toBeInTheDocument();
    // No disconnect banner — the host is alive, the chat stays open.
    expect(screen.queryByTestId("disconnected-indicator")).toBeNull();
  });

  it("renders nothing while liveness is unknown (pre-poll)", () => {
    const { container } = renderWithContext({ kind: "unknown" }, null);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the passive Connecting… band for a non-terminal-first starting session", () => {
    // A freshly-created regular session whose runner is spinning up gets a
    // muted, non-interactive heartbeat — NOT a banner, NOT a button.
    renderWithContext({ kind: "starting" }, null);
    const indicator = screen.getByTestId("connecting-indicator");
    expect(indicator).toBeInTheDocument();
    expect(indicator).toHaveTextContent(/connecting/i);
    expect(indicator.tagName).toBe("DIV");
    expect(indicator.querySelector(".animate-spin")).not.toBeNull();
    // It must not be the reconnect/fork banner.
    expect(screen.queryByTestId("disconnected-indicator")).toBeNull();
  });

  it("shows the Chat/Terminal pill (not the Connecting band) for a terminal-first starting session", () => {
    // Regression guard: terminal-first sessions keep their toggle during
    // spin-up — the pill's own terminal-pending spinner covers startup, so
    // the generic Connecting band must NOT take over here.
    renderWithContext(
      { kind: "starting" },
      makeCtx({ terminalsAvailable: false, terminalStartingUp: true }),
    );
    expect(screen.getByRole("group", { name: /view mode/i })).toBeInTheDocument();
    expect(screen.queryByTestId("connecting-indicator")).toBeNull();
    // The pill's own spin-up spinner is present.
    expect(
      screen.getByRole("button", { name: /^terminal$/i }).querySelector(".animate-spin"),
    ).not.toBeNull();
  });

  it("shows the Chat/Terminal pill for a terminal-first unknown (pre-poll) session", () => {
    // `unknown` reads as "assume online until proven otherwise" — the
    // toggle stays visible rather than flickering out before the first
    // poll resolves.
    renderWithContext({ kind: "unknown" }, makeCtx());
    expect(screen.getByRole("group", { name: /view mode/i })).toBeInTheDocument();
    expect(screen.queryByTestId("connecting-indicator")).toBeNull();
  });
});

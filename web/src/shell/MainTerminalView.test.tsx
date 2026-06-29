import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import { MainTerminalView } from "./MainTerminalView";
import type { TerminalFirstContextValue } from "./TerminalFirstContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";

vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({
    sessionId,
    terminalId,
    readOnly,
  }: {
    sessionId: string;
    terminalId: string;
    readOnly?: boolean;
  }) => (
    <div
      data-testid="terminal-view"
      data-session-id={sessionId}
      data-terminal-id={terminalId}
      data-read-only={String(readOnly ?? false)}
    />
  ),
}));

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (AGENT_TERMINAL_IDS, terminalTabKey) —
  // only the network-backed hook is replaced.
  ...(await importOriginal<typeof import("@/hooks/useTerminals")>()),
  useTerminals: vi.fn(),
}));

// Marker stand-in: MainTerminalView must NOT render the new-shell
// affordance in any state (creation lives in the rail's Shells tab) —
// the mock makes a regression visible if the import ever returns.
vi.mock("./NewTerminalButton", () => ({
  NewTerminalButton: () => <div data-testid="new-shell-button" />,
}));

const useTerminalsMock = vi.mocked(useTerminals);

const REPL_TERMINAL: TerminalInfo = {
  id: "terminal_tui_main",
  name: "tui",
  session: "main",
  running: true,
};
const BASH_SHELL: TerminalInfo = {
  id: "terminal_bash_s1",
  name: "bash",
  session: "s1",
  running: true,
};

/**
 * TerminalFirst context for the two session shapes under test — the
 * terminal-first SDK session and the native wrapper; both render the
 * agent terminal chrome-free and shells via the shell view.
 * `setView` is a spy so the shell view's close affordance is
 * assertable.
 */
function makeCtx(
  isNativeWrapper: boolean,
  setView: (view: "chat" | "terminal") => void = () => {},
): TerminalFirstContextValue {
  return {
    isClaudeNative: isNativeWrapper,
    isNativeWrapper,
    isTerminalFirst: true,
    isShellView: false,
    view: "terminal",
    terminalViewKey: null,
    setView,
    terminalsAvailable: true,
    terminalStartingUp: false,
  } as TerminalFirstContextValue;
}

function renderView({
  terminals,
  isNativeWrapper = false,
  initialTerminalKey = null,
  readOnly = false,
  setView,
}: {
  terminals: TerminalInfo[];
  isNativeWrapper?: boolean;
  initialTerminalKey?: string | null;
  readOnly?: boolean;
  setView?: (view: "chat" | "terminal") => void;
}) {
  useTerminalsMock.mockReturnValue({ terminals, isLoading: false, error: null });
  return render(
    <TerminalFirstContextProvider value={makeCtx(isNativeWrapper, setView)}>
      <MainTerminalView
        conversationId="conv_sdk"
        initialTerminalKey={initialTerminalKey}
        readOnly={readOnly}
      />
    </TerminalFirstContextProvider>,
  );
}

beforeEach(() => {
  useTerminalsMock.mockReset();
});

afterEach(cleanup);

describe("MainTerminalView — terminal-first SDK sessions", () => {
  it("renders the REPL chrome-free: shells and the + stay out of the pill view", () => {
    renderView({ terminals: [REPL_TERMINAL, BASH_SHELL] });

    // The agent's terminal fills the pane.
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_tui_main",
    );
    // No strip at all: a shell tab or the new-shell affordance here
    // means shells leaked back into the pill's Terminal section
    // (creation belongs to the rail's Shells tab).
    expect(screen.queryByText("bash")).toBeNull();
    expect(screen.queryByTestId("new-shell-button")).toBeNull();
  });

  it("forwards readOnly to the terminal so non-owners attach view-only", () => {
    // Owner (default): the agent terminal is interactive.
    const { unmount } = renderView({ terminals: [REPL_TERMINAL] });
    expect(screen.getByTestId("terminal-view")).toHaveAttribute("data-read-only", "false");
    unmount();

    // Non-owner: the same pane attaches read-only — they drive the
    // agent via the composer, since a shared PTY can't attribute
    // per-user keystrokes.
    renderView({ terminals: [REPL_TERMINAL], readOnly: true });
    expect(screen.getByTestId("terminal-view")).toHaveAttribute("data-read-only", "true");
  });

  it("forwards readOnly to a rail-opened shell too", () => {
    // A user shell shares the owner-only rule: non-owners watch but
    // can't type.
    renderView({
      terminals: [REPL_TERMINAL, BASH_SHELL],
      initialTerminalKey: "terminal:terminal_bash_s1",
      readOnly: true,
    });
    const view = screen.getByTestId("terminal-view");
    expect(view).toHaveAttribute("data-terminal-id", "terminal_bash_s1");
    expect(view).toHaveAttribute("data-read-only", "true");
  });

  it("renders a rail-opened shell chrome-free: shell header + close, no agent tab", () => {
    const setView = vi.fn();
    renderView({
      terminals: [REPL_TERMINAL, BASH_SHELL],
      initialTerminalKey: "terminal:terminal_bash_s1",
      setView,
    });

    // The shell replaced the view (this is the rail row's target).
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_bash_s1",
    );
    // The header names the shell only — an agent tab ("polly"/"tui")
    // here is the reported regression: the shell view must not imply
    // the shell is the agent.
    expect(screen.getByText("bash")).toBeInTheDocument();
    expect(screen.queryByText("tui")).toBeNull();
    expect(screen.queryByTestId("new-shell-button")).toBeNull();

    // The close X is the way back to chat (the Chat/Terminal pill is
    // hidden in shell view — ConnectionIndicator gates on isShellView).
    fireEvent.click(screen.getByRole("button", { name: "Close shell" }));
    expect(setView).toHaveBeenCalledWith("chat");
  });
});

describe("MainTerminalView — native wrapper sessions", () => {
  it("renders the vendor pane chrome-free, same as the SDK REPL", () => {
    const claudePane: TerminalInfo = {
      id: "terminal_claude_main",
      name: "claude",
      session: "main",
      running: true,
    };
    renderView({
      terminals: [claudePane, BASH_SHELL],
      isNativeWrapper: true,
    });

    // The vendor pane is the agent's terminal: it fills the pill view
    // with no strip, no shell tab, and no in-view creation — shells
    // live in the rail's Shells tab for native sessions too. A
    // "claude" tab or a + here means the old native strip came back.
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_claude_main",
    );
    expect(screen.queryByText("claude")).toBeNull();
    expect(screen.queryByText("bash")).toBeNull();
    expect(screen.queryByTestId("new-shell-button")).toBeNull();
  });

  it("renders a rail-opened shell chrome-free with the close X", () => {
    const claudePane: TerminalInfo = {
      id: "terminal_claude_main",
      name: "claude",
      session: "main",
      running: true,
    };
    const setView = vi.fn();
    renderView({
      terminals: [claudePane, BASH_SHELL],
      isNativeWrapper: true,
      initialTerminalKey: "terminal:terminal_bash_s1",
      setView,
    });

    // Same shell-view contract as SDK sessions: shell header only, no
    // vendor-pane tab, X back to chat.
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_bash_s1",
    );
    expect(screen.queryByText("claude")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close shell" }));
    expect(setView).toHaveBeenCalledWith("chat");
  });
});

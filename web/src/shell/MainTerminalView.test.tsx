import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import { MainTerminalView } from "./MainTerminalView";
import type { TerminalFirstContextValue } from "./TerminalFirstContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";

// Monotonic per-mount id. A fresh value on `data-instance` means React
// remounted the TerminalView (new xterm + WebSocket) rather than reusing
// the existing one — the signal the stale-scrollback regression test needs.
let terminalMountSeq = 0;

vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({
    sessionId,
    terminalId,
    readOnly,
  }: {
    sessionId: string;
    terminalId: string;
    readOnly?: boolean;
  }) => {
    // Assign once per mount (useRef(arg) evaluates arg every render but keeps
    // the first value), so the id is stable across re-renders and only changes
    // on a remount.
    const instance = useRef<number | null>(null);
    if (instance.current === null) instance.current = ++terminalMountSeq;
    return (
      <div
        data-testid="terminal-view"
        data-session-id={sessionId}
        data-terminal-id={terminalId}
        data-read-only={String(readOnly ?? false)}
        data-instance={String(instance.current)}
      />
    );
  },
}));

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (AGENT_TERMINAL_IDS, terminalTabKey) —
  // only the network-backed hook is replaced.
  ...(await importOriginal<typeof UseTerminalsModule>()),
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
  conversationId = "conv_sdk",
  setView,
}: {
  terminals: TerminalInfo[];
  isNativeWrapper?: boolean;
  initialTerminalKey?: string | null;
  readOnly?: boolean;
  conversationId?: string;
  setView?: (view: "chat" | "terminal") => void;
}) {
  useTerminalsMock.mockReturnValue({ terminals, isLoading: false, error: null });
  return render(
    <TerminalFirstContextProvider value={makeCtx(isNativeWrapper, setView)}>
      <MainTerminalView
        conversationId={conversationId}
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

  it("remounts the terminal when switching between two same-vendor sessions", () => {
    // Two claude-native sessions share the same agent-terminal id
    // (`terminal_claude_main`). ChatPage stays mounted across a session
    // switch and only feeds MainTerminalView a new conversationId, so the
    // terminal must remount off the session — otherwise the pane keeps the
    // previous session's scrollback until the new WS repaints.
    const claudePane: TerminalInfo = {
      id: "terminal_claude_main",
      name: "claude",
      session: "main",
      running: true,
    };
    const { rerender } = renderView({
      terminals: [claudePane],
      isNativeWrapper: true,
      conversationId: "conv_a",
    });
    const first = screen.getByTestId("terminal-view").getAttribute("data-instance");

    rerender(
      <TerminalFirstContextProvider value={makeCtx(true)}>
        <MainTerminalView conversationId="conv_b" initialTerminalKey={null} readOnly={false} />
      </TerminalFirstContextProvider>,
    );

    const view = screen.getByTestId("terminal-view");
    expect(view).toHaveAttribute("data-session-id", "conv_b");
    // A new instance id proves the mount was torn down and rebuilt for the
    // new session rather than reused with stale scrollback.
    expect(view.getAttribute("data-instance")).not.toBe(first);
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

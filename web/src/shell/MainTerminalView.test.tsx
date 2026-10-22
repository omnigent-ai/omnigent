import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useLayoutEffect, useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { readSessionWorkspaceState, writeSessionWorkspaceState } from "@/lib/sessionWorkspaceState";
import { updateTerminalSplit } from "./terminalSplitPanesStore";
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
    active,
    directAttachUrl,
    onResume,
    resumeTarget,
    resumePending,
  }: {
    sessionId: string;
    terminalId: string;
    readOnly?: boolean;
    active?: boolean;
    directAttachUrl?: string;
    onResume?: () => void | Promise<void>;
    resumeTarget?: string;
    resumePending?: boolean;
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
        data-active={String(active ?? true)}
        data-instance={String(instance.current)}
        data-direct-attach-url={directAttachUrl ?? ""}
        data-resume-pending={String(resumePending ?? false)}
        data-resume-target={resumeTarget ?? ""}
      >
        {onResume && (
          <button type="button" onClick={() => void onResume()}>
            Resume terminal
          </button>
        )}
      </div>
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
  overrides: Partial<TerminalFirstContextValue> = {},
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
    ...overrides,
  } as TerminalFirstContextValue;
}

function viewTree({
  terminals,
  isNativeWrapper = false,
  initialTerminalKey = null,
  visible = true,
  readOnly = false,
  conversationId = "conv_sdk",
  runnerOnline,
  onResume,
  setView,
  terminalStartingUp = false,
  sandboxOffline = false,
}: {
  terminals: TerminalInfo[];
  isNativeWrapper?: boolean;
  initialTerminalKey?: string | null;
  visible?: boolean;
  readOnly?: boolean;
  conversationId?: string;
  runnerOnline?: boolean;
  onResume?: () => void | Promise<void>;
  setView?: (view: "chat" | "terminal") => void;
  terminalStartingUp?: boolean;
  sandboxOffline?: boolean;
}) {
  useTerminalsMock.mockReturnValue({ terminals, isLoading: false, error: null });
  return (
    <TerminalFirstContextProvider value={makeCtx(isNativeWrapper, setView, { terminalStartingUp })}>
      <MainTerminalView
        conversationId={conversationId}
        initialTerminalKey={initialTerminalKey}
        visible={visible}
        readOnly={readOnly}
        runnerOnline={runnerOnline}
        onResume={onResume}
        sandboxOffline={sandboxOffline}
      />
    </TerminalFirstContextProvider>
  );
}

function renderView(args: Parameters<typeof viewTree>[0]) {
  return render(viewTree(args));
}

beforeEach(() => {
  useTerminalsMock.mockReset();
});

afterEach(cleanup);

describe("MainTerminalView — terminal-first SDK sessions", () => {
  it("renders the REPL chrome-free: shells and the + stay out of the pill view", async () => {
    renderView({ terminals: [REPL_TERMINAL, BASH_SHELL] });

    // The agent's terminal fills the pane. findByTestId waits for the lazy
    // TerminalView chunk to resolve through its Suspense boundary.
    expect(await screen.findByTestId("terminal-view")).toHaveAttribute(
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

  it("never substitutes an open user shell for the agent terminal", () => {
    renderView({ terminals: [BASH_SHELL] });

    expect(screen.queryByTestId("terminal-view")).toBeNull();
    expect(screen.getByText("Agent terminal unavailable.")).toBeInTheDocument();
  });

  it("treats an empty terminal inventory as resumable before health catches up", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    renderView({ terminals: [], onResume });

    expect(screen.getByText("The harness is not running.")).toBeInTheDocument();
    expect(screen.getByText("Resume the session to reconnect the terminal.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume session" }));

    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
  });

  it("names an offline sandbox and its resume action", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("launch failed"));
    renderView({ terminals: [], runnerOnline: false, onResume, sandboxOffline: true });

    expect(screen.getByText("The sandbox is offline.")).toBeInTheDocument();
    expect(screen.getByText("Resume it to reconnect the terminal.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resume sandbox" }));

    expect(await screen.findByText("Couldn't resume sandbox: launch failed")).toBeInTheDocument();
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("names the sandbox in an attached terminal's resume action", async () => {
    renderView({
      terminals: [REPL_TERMINAL],
      runnerOnline: false,
      onResume: vi.fn().mockResolvedValue(undefined),
      sandboxOffline: true,
    });

    expect(await screen.findByTestId("terminal-view")).toHaveAttribute(
      "data-resume-target",
      "sandbox",
    );
  });

  it("keeps the offline state actionable when resuming fails", async () => {
    const onResume = vi.fn().mockRejectedValue(new Error("host offline"));
    renderView({ terminals: [], runnerOnline: false, onResume });

    fireEvent.click(screen.getByRole("button", { name: "Resume session" }));

    expect(await screen.findByText("Couldn't resume session: host offline")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume session" })).toBeEnabled();
  });
});

describe("MainTerminalView — terminal startup in progress", () => {
  it("shows a passive loading status, never Resume, while a fresh session initializes", () => {
    // The reported regression: a fresh terminal-first session with no PTY
    // yet must render the passive startup state, never the actionable
    // stopped-session UI — even when the poll reports the runner down.
    const onResume = vi.fn().mockResolvedValue(undefined);
    renderView({ terminals: [], runnerOnline: false, onResume, terminalStartingUp: true });

    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Starting up…");
    expect(screen.queryByText("The harness is not running.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume session" })).toBeNull();
    expect(onResume).not.toHaveBeenCalled();
  });

  it("keeps a genuinely stopped session resumable once startup has settled", () => {
    // Guards the stopped-session behavior: an idle stopped session (not
    // starting) keeps the Resume action for the same empty inventory.
    const onResume = vi.fn().mockResolvedValue(undefined);
    renderView({ terminals: [], runnerOnline: false, onResume, terminalStartingUp: false });

    expect(screen.getByText("The harness is not running.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume session" })).toBeEnabled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("replaces the loading state with the terminal once the PTY appears", () => {
    const { rerender } = renderView({ terminals: [], terminalStartingUp: true });
    expect(screen.getByRole("status")).toHaveTextContent("Starting up…");

    rerender(viewTree({ terminals: [REPL_TERMINAL], terminalStartingUp: false }));

    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_tui_main",
    );
  });

  it("renders the arrived PTY in the same commit — no stopped/empty flash", () => {
    // activeKey normalizes in a passive effect one commit after the PTY
    // lands; a layout-effect probe captures each commit's DOM before it
    // runs — the transient frame must be the terminal, never stopped/empty.
    const commits: string[] = [];
    function CommitProbe() {
      useLayoutEffect(() => {
        commits.push(
          document.querySelector('[data-testid="main-terminal-view"]')?.textContent ?? "",
        );
      });
      return null;
    }
    const onResume = vi.fn().mockResolvedValue(undefined);
    const tree = (terminalStartingUp: boolean) => (
      <TerminalFirstContextProvider value={makeCtx(false, () => {}, { terminalStartingUp })}>
        <MainTerminalView conversationId="conv_sdk" runnerOnline={false} onResume={onResume} />
        <CommitProbe />
      </TerminalFirstContextProvider>
    );
    useTerminalsMock.mockReturnValue({ terminals: [], isLoading: false, error: null });
    const { rerender } = render(tree(true));
    expect(screen.getByRole("status")).toHaveTextContent("Starting up…");

    useTerminalsMock.mockReturnValue({
      terminals: [REPL_TERMINAL],
      isLoading: false,
      error: null,
    });
    rerender(tree(false));

    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_tui_main",
    );
    expect(commits.some((text) => text.includes("The harness is not running."))).toBe(false);
    expect(commits.some((text) => text.includes("Agent terminal unavailable."))).toBe(false);
    expect(screen.queryByRole("button", { name: "Resume session" })).toBeNull();
    expect(onResume).not.toHaveBeenCalled();
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

  it("re-attaches the agent terminal when a host switch resets the terminals cache", () => {
    // SwitchHostDialog resets the terminals cache after a successful switch:
    // a synchronous clear to [] followed by an invalidate/refetch
    // (web/src/shell/SwitchHostDialog.tsx). The agent terminal keeps the
    // same resource id across hosts, so the keyed mount only tears down
    // because of the empty intermediate render — if MainTerminalView ever
    // kept the pane alive across an empty inventory, the pill's Terminal
    // view would stay attached to the previous host's PTY (old WS, old
    // scrollback) after "Switch host…". This pins the reset's other half:
    // the clear unmounts the pane and the refetched row (the new host's
    // attach info) rebuilds it.
    const paneOnHostA: TerminalInfo = {
      id: "terminal_claude_main",
      name: "claude",
      session: "main",
      running: true,
      directAttachUrl: "ws://127.0.0.1:40001/?token=host-a",
    };
    const paneOnHostB: TerminalInfo = {
      ...paneOnHostA,
      directAttachUrl: "ws://127.0.0.1:40002/?token=host-b",
    };
    const { rerender } = renderView({
      terminals: [paneOnHostA],
      isNativeWrapper: true,
      conversationId: "conv_switch",
    });
    const first = screen.getByTestId("terminal-view").getAttribute("data-instance");

    // The dialog's setQueryData(…, []) lands synchronously: the old host's
    // pane must unmount (dropping its WebSocket), not linger.
    rerender(viewTree({ terminals: [], isNativeWrapper: true, conversationId: "conv_switch" }));
    expect(screen.queryByTestId("terminal-view")).toBeNull();

    // …then the invalidate's refetch delivers the new host's pane.
    rerender(
      viewTree({
        terminals: [paneOnHostB],
        isNativeWrapper: true,
        conversationId: "conv_switch",
      }),
    );
    const view = screen.getByTestId("terminal-view");
    // A fresh instance id proves a new mount — a new xterm + WebSocket
    // attach — carrying the new host's attach info.
    expect(view.getAttribute("data-instance")).not.toBe(first);
    expect(view).toHaveAttribute("data-direct-attach-url", "ws://127.0.0.1:40002/?token=host-b");
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

describe("MainTerminalView — persistent hidden mount", () => {
  it("toggles inert without remounting the terminal across a hide/show flip", () => {
    // ChatPage keeps this surface mounted as a hidden overlay while the
    // user is in chat. A new data-instance after the round-trip means
    // the flip tore down the xterm + WS it exists to preserve.
    const { rerender } = renderView({ terminals: [REPL_TERMINAL] });
    const view = screen.getByTestId("terminal-view");
    const instance = view.getAttribute("data-instance");
    expect(screen.getByTestId("main-terminal-view")).toHaveAttribute("data-visible", "true");
    expect(screen.getByTestId("main-terminal-view")).not.toHaveAttribute("inert");

    rerender(
      <TerminalFirstContextProvider value={makeCtx(false)}>
        <MainTerminalView
          conversationId="conv_sdk"
          initialTerminalKey={null}
          visible={false}
          readOnly={false}
        />
      </TerminalFirstContextProvider>,
    );
    expect(screen.getByTestId("main-terminal-view")).toHaveAttribute("data-visible", "false");
    expect(screen.getByTestId("main-terminal-view")).toHaveAttribute("inert", "");
    expect(screen.getByTestId("terminal-view").getAttribute("data-instance")).toBe(instance);

    rerender(
      <TerminalFirstContextProvider value={makeCtx(false)}>
        <MainTerminalView
          conversationId="conv_sdk"
          initialTerminalKey={null}
          visible
          readOnly={false}
        />
      </TerminalFirstContextProvider>,
    );
    expect(screen.getByTestId("main-terminal-view")).toHaveAttribute("data-visible", "true");
    expect(screen.getByTestId("main-terminal-view")).not.toHaveAttribute("inert");
    expect(screen.getByTestId("terminal-view").getAttribute("data-instance")).toBe(instance);
  });

  it("makes an initially hidden pre-warmed terminal inert on mount", () => {
    renderView({ terminals: [REPL_TERMINAL], visible: false });

    expect(screen.getByTestId("main-terminal-view")).toHaveAttribute("data-visible", "false");
    expect(screen.getByTestId("main-terminal-view")).toHaveAttribute("inert", "");
    expect(screen.getByTestId("terminal-view")).toBeInTheDocument();
  });

  it("falls back to the agent pane when the restored target no longer exists", () => {
    // `?view=terminal` resolves against the per-session stored panel key, which
    // can name a shell that has since been closed. The stale key must degrade to
    // the agent terminal, not leave the pane empty.
    renderView({
      terminals: [REPL_TERMINAL],
      initialTerminalKey: "terminal:terminal_bash_gone",
    });

    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_tui_main",
    );
    // No shell header: the pane is the agent's, not a phantom shell.
    expect(screen.queryByRole("button", { name: "Close shell" })).toBeNull();
  });

  it("resets a shell selection to the agent terminal while hidden", () => {
    // Open on a rail shell, then close the view (AppShell nulls the
    // target key when the view closes). The old unmount-on-close forgot
    // the shell selection, so reopening always showed the agent pane —
    // the persistent mount must reproduce that.
    const { rerender } = renderView({
      terminals: [REPL_TERMINAL, BASH_SHELL],
      initialTerminalKey: "terminal:terminal_bash_s1",
    });
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_bash_s1",
    );

    rerender(
      <TerminalFirstContextProvider value={makeCtx(false)}>
        <MainTerminalView
          conversationId="conv_sdk"
          initialTerminalKey={null}
          visible={false}
          readOnly={false}
        />
      </TerminalFirstContextProvider>,
    );
    // The hidden background attach now targets the agent terminal — the
    // pane the next open will actually show.
    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_tui_main",
    );
  });
});

describe("MainTerminalView — split panes", () => {
  const AGENT_KEY = "terminal:terminal_tui_main";
  const BASH_KEY = "terminal:terminal_bash_s1";
  const savedSplit = {
    layout: {
      axis: "columns" as const,
      ratio: 0.5,
      children: [AGENT_KEY, BASH_KEY] as [string, string],
    },
    focusedKey: BASH_KEY,
  };

  function splitTree(
    conversationId: string,
    terminals: TerminalInfo[],
    enabled = true,
    { isFetching = false, listCompleteAt = 0, visible = true, readOnly = false } = {},
  ) {
    useTerminalsMock.mockReturnValue({
      terminals,
      isLoading: false,
      isFetching,
      listCompleteAt,
      error: null,
    });
    return (
      <CapabilitiesProvider
        info={{ ...FALLBACK_SERVER_INFO, features: { splittable_terminal_view_ui: enabled } }}
      >
        <TerminalFirstContextProvider value={makeCtx(false)}>
          <MainTerminalView
            conversationId={conversationId}
            visible={visible}
            readOnly={readOnly}
            splittable
          />
        </TerminalFirstContextProvider>
      </CapabilitiesProvider>
    );
  }

  it("renders saved panes side by side with only the focused one active", async () => {
    writeSessionWorkspaceState("conv_split_render", { terminalSplit: savedSplit });
    render(splitTree("conv_split_render", [REPL_TERMINAL, BASH_SHELL]));

    await waitFor(() => expect(screen.getAllByTestId("terminal-view")).toHaveLength(2));
    const [agentPane, bashPane] = screen.getAllByTestId("terminal-view");
    expect(agentPane).toHaveAttribute("data-terminal-id", "terminal_tui_main");
    expect(agentPane).toHaveAttribute("data-active", "false");
    expect(bashPane).toHaveAttribute("data-active", "true");
    expect(screen.getByRole("separator", { name: "Resize terminal panes" })).toBeInTheDocument();
  });

  it("ignores a saved split while the feature is off", async () => {
    writeSessionWorkspaceState("conv_split_off", { terminalSplit: savedSplit });
    render(splitTree("conv_split_off", [REPL_TERMINAL, BASH_SHELL], false));

    expect(await screen.findAllByTestId("terminal-view")).toHaveLength(1);
    expect(screen.queryByRole("separator")).toBeNull();
  });

  it("keeps the agent xterm mounted when a split appears", async () => {
    render(splitTree("conv_split_mount", [REPL_TERMINAL, BASH_SHELL]));
    const before = (await screen.findByTestId("terminal-view")).getAttribute("data-instance");

    act(() => updateTerminalSplit("conv_split_mount", () => savedSplit));

    await waitFor(() => expect(screen.getAllByTestId("terminal-view")).toHaveLength(2));
    expect(screen.getAllByTestId("terminal-view")[0]).toHaveAttribute("data-instance", before);
  });

  it("drops a closed shell's pane and returns to the single agent view", async () => {
    writeSessionWorkspaceState("conv_split_prune", { terminalSplit: savedSplit });
    const view = render(splitTree("conv_split_prune", [REPL_TERMINAL, BASH_SHELL]));
    view.rerender(splitTree("conv_split_prune", [REPL_TERMINAL]));

    await waitFor(() =>
      expect(readSessionWorkspaceState("conv_split_prune").terminalSplit).toBeUndefined(),
    );
    expect(screen.getAllByTestId("terminal-view")).toHaveLength(1);
  });

  it("drops a never-seen shell's pane once a complete runner answer lands", async () => {
    writeSessionWorkspaceState("conv_split_stale", { terminalSplit: savedSplit });
    const view = render(splitTree("conv_split_stale", []));
    view.rerender(
      splitTree("conv_split_stale", [REPL_TERMINAL], true, { listCompleteAt: Date.now() + 1 }),
    );

    await waitFor(() =>
      expect(readSessionWorkspaceState("conv_split_stale").terminalSplit).toBeUndefined(),
    );
  });

  it("keeps a never-seen shell's pane while the list is not known complete", async () => {
    // A failed or soft-status fetch, or an SSE-only fill, leaves the list partial
    // with no fetch in flight and no complete answer.
    writeSessionWorkspaceState("conv_split_partial", { terminalSplit: savedSplit });
    render(splitTree("conv_split_partial", [REPL_TERMINAL]));

    expect(await screen.findByText("Terminal unavailable.")).toBeInTheDocument();
    expect(readSessionWorkspaceState("conv_split_partial").terminalSplit).toEqual(savedSplit);
  });

  it("does not trust a complete answer older than the last empty list", async () => {
    writeSessionWorkspaceState("conv_split_reset", { terminalSplit: savedSplit });
    const staleAnswer = Date.now() - 1;
    const view = render(splitTree("conv_split_reset", [], true, { listCompleteAt: staleAnswer }));
    view.rerender(
      splitTree("conv_split_reset", [REPL_TERMINAL], true, { listCompleteAt: staleAnswer }),
    );

    expect(await screen.findByText("Terminal unavailable.")).toBeInTheDocument();
    expect(readSessionWorkspaceState("conv_split_reset").terminalSplit).toEqual(savedSplit);
  });

  it("persists keyboard resizes within the 25-75% bounds", async () => {
    writeSessionWorkspaceState("conv_split_resize", { terminalSplit: savedSplit });
    render(splitTree("conv_split_resize", [REPL_TERMINAL, BASH_SHELL]));
    const divider = await screen.findByRole("separator", { name: "Resize terminal panes" });

    for (let step = 0; step < 10; step++) fireEvent.keyDown(divider, { key: "ArrowRight" });

    expect(divider).toHaveAttribute("aria-valuenow", "75");
    expect(readSessionWorkspaceState("conv_split_resize").terminalSplit?.layout).toMatchObject({
      ratio: 0.75,
    });
  });

  it("keeps a saved split while the terminal list is still filling in", async () => {
    writeSessionWorkspaceState("conv_split_reload", { terminalSplit: savedSplit });
    const view = render(
      splitTree("conv_split_reload", [REPL_TERMINAL], true, { isFetching: true }),
    );
    view.rerender(splitTree("conv_split_reload", [REPL_TERMINAL, BASH_SHELL]));

    await waitFor(() => expect(screen.getAllByTestId("terminal-view")).toHaveLength(2));
    expect(readSessionWorkspaceState("conv_split_reload").terminalSplit).toEqual(savedSplit);
  });

  it("keeps live shell panes while the agent terminal is missing", async () => {
    writeSessionWorkspaceState("conv_split_no_agent", { terminalSplit: savedSplit });
    render(splitTree("conv_split_no_agent", [BASH_SHELL]));

    expect(await screen.findByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_bash_s1",
    );
    expect(screen.getByText("Terminal unavailable.")).toBeInTheDocument();
  });

  it("moves a stale agent pane onto the live agent terminal", async () => {
    writeSessionWorkspaceState("conv_split_agent_swap", { terminalSplit: savedSplit });
    const claude = { id: "terminal_claude_main", name: "claude", session: "main", running: true };
    render(splitTree("conv_split_agent_swap", [claude, BASH_SHELL]));

    await waitFor(() =>
      expect(readSessionWorkspaceState("conv_split_agent_swap").terminalSplit?.layout).toEqual({
        ...savedSplit.layout,
        children: ["terminal:terminal_claude_main", BASH_KEY],
      }),
    );
  });

  it("renders hidden and read-only surfaces unsplit", async () => {
    writeSessionWorkspaceState("conv_split_gates", { terminalSplit: savedSplit });
    const terminals = [REPL_TERMINAL, BASH_SHELL];
    const view = render(splitTree("conv_split_gates", terminals, true, { visible: false }));
    expect(await screen.findAllByTestId("terminal-view")).toHaveLength(1);
    view.rerender(splitTree("conv_split_gates", terminals, true, { readOnly: true }));
    expect(screen.getAllByTestId("terminal-view")).toHaveLength(1);
    expect(readSessionWorkspaceState("conv_split_gates").terminalSplit).toEqual(savedSplit);
  });

  it("moves split focus to the pane the user focuses", async () => {
    writeSessionWorkspaceState("conv_split_focus", { terminalSplit: savedSplit });
    const { container } = render(splitTree("conv_split_focus", [REPL_TERMINAL, BASH_SHELL]));
    await waitFor(() => expect(screen.getAllByTestId("terminal-view")).toHaveLength(2));

    fireEvent.focus(container.querySelector(`[data-terminal-pane="${AGENT_KEY}"]`)!);

    expect(readSessionWorkspaceState("conv_split_focus").terminalSplit?.focusedKey).toBe(AGENT_KEY);
    expect(screen.getAllByTestId("terminal-view")[0]).toHaveAttribute("data-active", "true");
  });

  it("commits a pointer drag onto the latest layout, ignoring cross-axis keys", async () => {
    writeSessionWorkspaceState("conv_split_drag", { terminalSplit: savedSplit });
    const { container } = render(splitTree("conv_split_drag", [REPL_TERMINAL, BASH_SHELL]));
    const divider = await screen.findByRole("separator", { name: "Resize terminal panes" });
    const panes = divider.parentElement!;
    vi.spyOn(panes, "getBoundingClientRect").mockReturnValue({
      left: 0,
      top: 0,
      width: 1000,
      height: 500,
    } as DOMRect);
    let captured = false;
    divider.setPointerCapture = () => {
      captured = true;
    };
    divider.hasPointerCapture = () => captured;

    fireEvent.keyDown(divider, { key: "ArrowDown" });
    expect(divider).toHaveAttribute("aria-valuenow", "50");
    fireEvent.pointerDown(divider, { pointerId: 1 });
    fireEvent.pointerMove(divider, { pointerId: 1, clientX: 650, clientY: 100 });
    expect(divider).toHaveAttribute("aria-valuenow", "65");
    expect(readSessionWorkspaceState("conv_split_drag").terminalSplit?.layout).toMatchObject({
      ratio: 0.5,
    });
    fireEvent.pointerUp(divider, { pointerId: 1 });

    expect(readSessionWorkspaceState("conv_split_drag").terminalSplit?.layout).toMatchObject({
      ratio: 0.65,
    });
    expect(container.querySelector(`[data-terminal-pane="${BASH_KEY}"]`)).toHaveStyle({
      left: "65%",
    });
  });
});

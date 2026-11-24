import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { TerminalLaunchCallbacks } from "@/hooks/useTerminals";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import { setTerminalLaunching } from "@/lib/terminals";

import {
  isFailedPaneStart,
  recordPaneStartFailure,
  resetPaneStartFailuresForTests,
} from "./paneStartFailures";
import { PREFERRED_SHELL_KEY, readPreferredShell } from "./preferredShell";
import { PANE_CLOSED_MESSAGE } from "./splitTargetActions";
import { resetPendingPaneClosesForTests, useTerminalPaneActions } from "./terminalPaneActions";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

const AGENT = { id: "terminal_claude_main", name: "claude", session: "main", running: true };
const ZSH = { id: "terminal_zsh_s1", name: "zsh", session: "s1", running: true };
const AGENT_KEY = "terminal:terminal_claude_main";
const ZSH_KEY = "terminal:terminal_zsh_s1";

const fixture = vi.hoisted(() => ({
  launch: vi.fn<(terminal: string, callbacks: TerminalLaunchCallbacks) => void>(),
  endShell: vi.fn<(terminalId: string) => void>(),
  toast: vi.fn<(message: string) => void>(),
  declaredShells: ["zsh", "bash"],
}));

vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: fixture.declaredShells } }),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useCreateTerminal: () => ({ launch: fixture.launch, isPending: false }),
  useDeleteTerminal: () => ({ mutate: fixture.endShell, isPending: false }),
}));
vi.mock("@/components/ui/toast", () => ({ showToast: fixture.toast }));

let session = 0;

/** A session's main split kept in memory, starting from `initial`. */
function memoryTarget(initial?: TerminalSplitState) {
  let state = initial;
  const conversationId = `session-actions-${++session}`;
  const target: TerminalSplitTarget = {
    id: `main:${conversationId}`,
    conversationId,
    read: () => state,
    update: (update) => {
      state = update(state);
    },
    unsplitKey: () => AGENT_KEY,
    canClosePane: (key) => key !== AGENT_KEY,
    focusSurface: "main",
  };
  return { target, read: () => state };
}

const SIDE_BY_SIDE: TerminalSplitState = {
  layout: { axis: "columns", ratio: 0.5, children: [AGENT_KEY, ZSH_KEY] },
  focusedKey: ZSH_KEY,
};

beforeEach(() => {
  fixture.launch.mockReset();
  fixture.endShell.mockReset();
  fixture.toast.mockReset();
  fixture.declaredShells = ["zsh", "bash"];
});
afterEach(() => {
  resetPaneStartFailuresForTests();
  resetPendingPaneClosesForTests();
  window.localStorage.clear();
});

describe("useTerminalPaneActions", () => {
  it("splits the lone agent pane with the default shell, the new pane taking focus", () => {
    const { target, read } = memoryTarget();
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    expect(result.current.shellOptions).toEqual(["zsh", "bash"]);
    act(() => result.current.splitPane(AGENT_KEY, "columns"));
    expect(fixture.launch).toHaveBeenCalledWith("zsh", expect.any(Object));
    const [, callbacks] = fixture.launch.mock.calls[0]!;
    act(() => callbacks.onOpen?.(ZSH));
    expect(read()).toEqual(SIDE_BY_SIDE);
    // The default isn't remembered as a pick.
    expect(window.localStorage.getItem(PREFERRED_SHELL_KEY)).toBeNull();
  });

  it("remembers a shell picked over the default", () => {
    const { target } = memoryTarget();
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    act(() => result.current.splitPane(AGENT_KEY, "rows", "bash"));
    expect(fixture.launch).toHaveBeenCalledWith("bash", expect.any(Object));
    expect(readPreferredShell()).toBe("bash");
  });

  it("starts no shell for a pane that's gone, and says so", () => {
    const { target } = memoryTarget(SIDE_BY_SIDE);
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT, ZSH]));
    act(() => result.current.splitPane("terminal:terminal_gone", "columns"));
    expect(fixture.launch).not.toHaveBeenCalled();
    expect(fixture.toast).toHaveBeenCalledWith(PANE_CLOSED_MESSAGE);
  });

  it("can't split where the agent declares no shell", () => {
    fixture.declaredShells = [];
    const { target } = memoryTarget();
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    expect(result.current.canSplit).toBe(false);
    act(() => result.current.splitPane(AGENT_KEY, "columns"));
    expect(fixture.launch).not.toHaveBeenCalled();
  });

  it("keeps a pane whose shell didn't start, saying why", () => {
    const { target, read } = memoryTarget();
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    act(() => result.current.splitPane(AGENT_KEY, "columns"));
    const [, callbacks] = fixture.launch.mock.calls[0]!;
    act(() => callbacks.onOpen?.(ZSH));
    act(() => callbacks.onError?.(new Error("No runner."), ZSH));
    expect(read()?.layout).toEqual(SIDE_BY_SIDE.layout);
    expect(isFailedPaneStart(target.conversationId, ZSH_KEY)).toBe(true);
    expect(fixture.toast).not.toHaveBeenCalled();
  });

  it("gives the server's shell the pane the guess opened, when its id differs", () => {
    const { target, read } = memoryTarget();
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    act(() => result.current.splitPane(AGENT_KEY, "columns"));
    const [, callbacks] = fixture.launch.mock.calls[0]!;
    act(() => callbacks.onOpen?.(ZSH));
    act(() => callbacks.onOpen?.({ ...ZSH, id: "terminal_zsh_s7", session: "s7" }));
    expect(read()).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT_KEY, "terminal:terminal_zsh_s7"] },
      focusedKey: "terminal:terminal_zsh_s7",
    });
  });

  it("leaves the keyboard where it was when a new pane's shell fails to start", () => {
    const { target, read } = memoryTarget();
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    act(() => result.current.splitPane(AGENT_KEY, "columns"));
    const [, callbacks] = fixture.launch.mock.calls[0]!;
    setTerminalLaunching(ZSH.id, "attach", target.conversationId);
    try {
      act(() => callbacks.onOpen?.(ZSH));
      act(() => callbacks.onError?.(new Error("No runner."), ZSH));
    } finally {
      act(() => setTerminalLaunching(ZSH.id, null));
    }
    expect(isFailedPaneStart(target.conversationId, ZSH_KEY)).toBe(true);
    expect(read()?.focusedKey).toBe(AGENT_KEY);
  });

  it("closes nothing from a stale row or dialog whose pane is gone, and says so", () => {
    const { target, read } = memoryTarget(SIDE_BY_SIDE);
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT, ZSH]));
    act(() => result.current.closePane("terminal:terminal_gone"));
    expect(result.current.pendingClose).toBeNull();
    expect(fixture.toast).toHaveBeenCalledWith(PANE_CLOSED_MESSAGE);

    // The dialog stays open while the split is undone: the shell now shows as its own tab.
    act(() => result.current.closePane(ZSH_KEY));
    act(() => target.update(() => undefined));
    act(() => result.current.confirmClose());
    expect(read()).toBeUndefined();
    expect(fixture.endShell).not.toHaveBeenCalled();
    expect(fixture.toast).toHaveBeenCalledTimes(2);
  });

  it("asks before ending a pane's shell, and ends it only once confirmed", () => {
    const { target, read } = memoryTarget(SIDE_BY_SIDE);
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT, ZSH]));
    act(() => result.current.closePane(ZSH_KEY));
    expect(result.current.pendingClose).toEqual({ key: ZSH_KEY, terminal: ZSH });
    act(() => result.current.cancelClose());
    expect(result.current.pendingClose).toBeNull();
    expect(read()).toEqual(SIDE_BY_SIDE);

    act(() => result.current.closePane(ZSH_KEY));
    act(() => result.current.confirmClose());
    expect(read()).toBeUndefined();
    expect(fixture.endShell).toHaveBeenCalledExactlyOnceWith(ZSH.id);
  });

  it("closes a pane whose shell didn't start without asking, since it ends nothing", () => {
    const { target, read } = memoryTarget(SIDE_BY_SIDE);
    recordPaneStartFailure(target.conversationId, ZSH_KEY, {
      terminal: "zsh",
      reason: "No runner.",
    });
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    act(() => result.current.closePane(ZSH_KEY));
    expect(result.current.pendingClose).toBeNull();
    expect(read()).toBeUndefined();
    expect(isFailedPaneStart(target.conversationId, ZSH_KEY)).toBe(false);
    expect(fixture.endShell).not.toHaveBeenCalled();
  });

  it("drops a pane whose shell is already gone at once, and never closes the agent's", () => {
    const { target, read } = memoryTarget(SIDE_BY_SIDE);
    const { result } = renderHook(() => useTerminalPaneActions(target, [AGENT]));
    act(() => result.current.closePane(AGENT_KEY));
    expect(read()).toEqual(SIDE_BY_SIDE);
    act(() => result.current.closePane(ZSH_KEY));
    expect(result.current.pendingClose).toBeNull();
    expect(read()).toBeUndefined();
    expect(fixture.endShell).not.toHaveBeenCalled();
  });

  it("shares a split's pending close between the hosts that offer it", () => {
    const { target } = memoryTarget(SIDE_BY_SIDE);
    const first = renderHook(() => useTerminalPaneActions(target, [AGENT, ZSH]));
    const second = renderHook(() => useTerminalPaneActions(target, [AGENT, ZSH]));
    act(() => first.result.current.closePane(ZSH_KEY));
    expect(second.result.current.pendingClose?.key).toBe(ZSH_KEY);
  });
});

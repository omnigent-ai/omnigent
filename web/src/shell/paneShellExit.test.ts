import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";

import {
  isPaneShellEnded,
  resetEndedPaneShellsForTests,
  shellEndedOnClose,
  usePaneShellExit,
} from "./paneShellExit";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

const AGENT_KEY = "terminal:terminal_claude_main";
const ZSH_KEY = "terminal:terminal_zsh_s1";
const BASH_KEY = "terminal:terminal_bash_s2";

const CONNECTED: ConnectionState = { kind: "connected" };
const ENDED: ConnectionState = { kind: "closed", code: 4404, reason: "terminal session ended" };
const DROPPED: ConnectionState = { kind: "closed", code: 1006, reason: "code 1006" };
const DETACHED: ConnectionState = { kind: "closed", code: 4405, reason: "terminal detached" };
// The attach routes accept the socket before looking the terminal up, so a refusal follows a connect.
const REFUSED: ConnectionState = {
  kind: "closed",
  code: 4404,
  reason: "terminal resource not found or not running",
};

const THREE_PANES: TerminalSplitState = {
  layout: {
    axis: "columns",
    ratio: 0.5,
    children: [AGENT_KEY, { axis: "rows", ratio: 0.5, children: [ZSH_KEY, BASH_KEY] }],
  },
  focusedKey: ZSH_KEY,
};

let session = 0;
function memoryTarget(initial: TerminalSplitState) {
  let state: TerminalSplitState | undefined = initial;
  const conversationId = `pane-exit-${++session}`;
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

const panesOf = (state: TerminalSplitState | undefined): string[] =>
  JSON.stringify(state?.layout ?? null).match(/terminal:[\w-]+/g) ?? [];

afterEach(resetEndedPaneShellsForTests);

describe("shellEndedOnClose", () => {
  it.each([
    ["the server ends a connected attach as gone", ENDED, true, true],
    ["the attach was never connected, as a shell not listed yet", ENDED, false, false],
    ["the transport drops and reconnects", DROPPED, true, false],
    ["another client takes the terminal over", DETACHED, true, false],
    ["the server refuses an attach it accepted", REFUSED, true, false],
    [
      "the registry isn't ready",
      { ...REFUSED, reason: "terminal registry not available" },
      true,
      false,
    ],
    ["the pane connects", CONNECTED, true, false],
    ["the pane unmounts", null, true, false],
  ])("is %s: %s", (_case, state, wasConnected, ended) => {
    expect(shellEndedOnClose(state, wasConnected)).toBe(ended);
  });
});

describe("usePaneShellExit", () => {
  it("removes a pane whose shell exits and gives its neighbour the keyboard", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target));
    act(() => result.current(ZSH_KEY, CONNECTED));
    act(() => result.current(ZSH_KEY, ENDED));
    expect(panesOf(read())).toEqual([AGENT_KEY, BASH_KEY]);
    expect(read()?.focusedKey).not.toBe(ZSH_KEY);
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(true);
    // A shell relaunched under the same key connects again, and is no longer taken as ended.
    act(() => result.current(ZSH_KEY, CONNECTED));
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
  });

  it("keeps a pane closed before it ever connected, or dropped by the transport", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target));
    act(() => result.current(ZSH_KEY, ENDED));
    act(() => result.current(BASH_KEY, CONNECTED));
    act(() => result.current(BASH_KEY, DROPPED));
    expect(read()).toEqual(THREE_PANES);
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
  });

  it("keeps a pane whose attach is refused after connecting, across a reconnect", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target));
    for (let attempt = 0; attempt < 2; attempt += 1) {
      act(() => result.current(ZSH_KEY, CONNECTED));
      act(() => result.current(ZSH_KEY, REFUSED));
    }
    expect(read()).toEqual(THREE_PANES);
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
  });

  it("keeps the agent's own pane when its terminal ends", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target));
    act(() => result.current(AGENT_KEY, CONNECTED));
    act(() => result.current(AGENT_KEY, ENDED));
    expect(read()).toEqual(THREE_PANES);
  });
});

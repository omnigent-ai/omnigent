import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ConnectionState } from "@/components/blocks/TerminalSession";
import { type TerminalSplitState, terminalLayoutKeys } from "@/lib/paneSplitLayout";

import {
  isPaneShellEnded,
  markPaneShellEnded,
  resetEndedPaneShellsForTests,
  shellEndedOnClose,
  usePaneShellExit,
} from "./paneShellExit";
import { pendingTerminalFocus } from "./terminalFocusRequest";
import type { TerminalSplitTarget } from "./terminalSplitTarget";
import { usePaneKeyboardOwner } from "./usePaneKeyboardOwner";

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
  state ? terminalLayoutKeys(state.layout) : [];

const noOwner = () => null;

/** A panes host with an input in each pane, and an input outside it, as a sidebar editor. */
function panesHost(keys: readonly string[]) {
  const host = document.createElement("div");
  const inputs = new Map<string, HTMLInputElement>();
  for (const key of keys) {
    const pane = document.createElement("div");
    pane.dataset.terminalPane = key;
    const input = document.createElement("input");
    pane.append(input);
    host.append(pane);
    inputs.set(key, input);
  }
  const outside = document.createElement("input");
  document.body.append(host, outside);
  return { hostRef: { current: host }, inputs, outside };
}

afterEach(() => {
  resetEndedPaneShellsForTests();
  document.body.replaceChildren();
});

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
  it("removes a pane whose shell exits and gives its neighbour the keyboard it had", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { hostRef, inputs } = panesHost(panesOf(THREE_PANES));
    const { result } = renderHook(() => {
      const owner = usePaneKeyboardOwner(hostRef);
      return usePaneShellExit(target, true, owner);
    });
    act(() => inputs.get(ZSH_KEY)?.focus());
    act(() => result.current(ZSH_KEY, CONNECTED));
    act(() => result.current(ZSH_KEY, ENDED));
    expect(panesOf(read())).toEqual([AGENT_KEY, BASH_KEY]);
    expect(read()?.focusedKey).not.toBe(ZSH_KEY);
    expect(pendingTerminalFocus(target.conversationId, "main")).toMatchObject({
      tabKey: read()?.focusedKey,
    });
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(true);
    // A shell relaunched under the same key connects again, and is no longer taken as ended.
    act(() => result.current(ZSH_KEY, CONNECTED));
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
  });

  it("removes a pane whose shell exits but leaves the keyboard with an input outside the panes", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { hostRef, inputs, outside } = panesHost(panesOf(THREE_PANES));
    const { result } = renderHook(() => {
      const owner = usePaneKeyboardOwner(hostRef);
      return usePaneShellExit(target, true, owner);
    });
    // The shell had the keyboard, then the user moved to an editor before it exited.
    act(() => inputs.get(ZSH_KEY)?.focus());
    act(() => result.current(ZSH_KEY, CONNECTED));
    act(() => outside.focus());
    act(() => result.current(ZSH_KEY, ENDED));
    expect(panesOf(read())).toEqual([AGENT_KEY, BASH_KEY]);
    expect(pendingTerminalFocus(target.conversationId, "main")).toBeNull();
    expect(document.activeElement).toBe(outside);
  });

  it("without removal, leaves an exited pane and marks nothing, but still forgets on connect", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target, false, noOwner));
    act(() => result.current(ZSH_KEY, CONNECTED));
    act(() => result.current(ZSH_KEY, ENDED));
    expect(read()).toEqual(THREE_PANES);
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
    markPaneShellEnded(target.conversationId, BASH_KEY);
    act(() => result.current(BASH_KEY, CONNECTED));
    expect(isPaneShellEnded(target.conversationId, BASH_KEY)).toBe(false);
  });

  it("keeps a pane closed before it ever connected, or dropped by the transport", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target, true, noOwner));
    act(() => result.current(ZSH_KEY, ENDED));
    act(() => result.current(BASH_KEY, CONNECTED));
    act(() => result.current(BASH_KEY, DROPPED));
    expect(read()).toEqual(THREE_PANES);
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
  });

  it("keeps a pane whose attach is refused after connecting, across a reconnect", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target, true, noOwner));
    for (let attempt = 0; attempt < 2; attempt += 1) {
      act(() => result.current(ZSH_KEY, CONNECTED));
      act(() => result.current(ZSH_KEY, REFUSED));
    }
    expect(read()).toEqual(THREE_PANES);
    expect(isPaneShellEnded(target.conversationId, ZSH_KEY)).toBe(false);
  });

  it("keeps the agent's own pane when its terminal ends", () => {
    const { target, read } = memoryTarget(THREE_PANES);
    const { result } = renderHook(() => usePaneShellExit(target, true, noOwner));
    act(() => result.current(AGENT_KEY, CONNECTED));
    act(() => result.current(AGENT_KEY, ENDED));
    expect(read()).toEqual(THREE_PANES);
  });
});

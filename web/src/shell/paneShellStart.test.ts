import { afterEach, describe, expect, it, vi } from "vitest";

import type { TerminalInfo, TerminalLaunchCallbacks } from "@/hooks/useTerminals";
import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import { TERMINAL_SURFACE_ATTRIBUTE } from "@/lib/paneSurfaces";
import { setTerminalLaunching } from "@/lib/terminals";

import { failedPaneMayTakeFocus, keepFailedPaneStart, retryPaneStart } from "./paneShellStart";
import {
  isFailedPaneStart,
  paneStartFailure,
  recordPaneStartFailure,
  resetPaneStartFailuresForTests,
} from "./paneStartFailures";
import { pendingTerminalFocus } from "./terminalFocusRequest";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

const AGENT = "terminal:terminal_claude_main";
const FAILED = "terminal:terminal_zsh_s1";
const shell = (id: string, name = "zsh"): TerminalInfo => ({
  id,
  name,
  session: id.split("_").pop()!,
  running: true,
});

/** A main-view split kept in memory, starting as the agent beside a failed zsh. */
function memoryTarget(conversationId = "session-start") {
  let state: TerminalSplitState | undefined = {
    layout: { axis: "columns", ratio: 0.5, children: [AGENT, FAILED] },
    focusedKey: FAILED,
  };
  const target: TerminalSplitTarget = {
    id: `main:${conversationId}`,
    conversationId,
    read: () => state,
    update: (update) => {
      state = update(state);
    },
    unsplitKey: () => AGENT,
    canClosePane: (key) => key !== AGENT,
    focusSurface: "main",
  };
  return { target, read: () => state };
}

afterEach(() => resetPaneStartFailuresForTests());

describe("a failed pane start", () => {
  it("keeps its slot with why it failed, but only while the slot is in the split", () => {
    const { target } = memoryTarget();
    expect(keepFailedPaneStart(target, shell("terminal_zsh_s1"), new Error("No runner."))).toBe(
      true,
    );
    expect(paneStartFailure(target.conversationId, FAILED)).toEqual({
      terminal: "zsh",
      reason: "No runner.",
    });
    expect(keepFailedPaneStart(target, shell("terminal_zsh_gone"), new Error("x"))).toBe(false);
  });

  it("tries again into the same slot, which takes the new shell's key", () => {
    const { target, read } = memoryTarget();
    recordPaneStartFailure(target.conversationId, FAILED, { terminal: "zsh", reason: "x" });
    const launch = vi.fn<(terminal: string, callbacks: TerminalLaunchCallbacks) => void>();
    expect(retryPaneStart(target, FAILED, launch)).toBe(true);
    expect(launch).toHaveBeenCalledWith("zsh", expect.any(Object));
    expect(isFailedPaneStart(target.conversationId, FAILED)).toBe(false);

    const [, callbacks] = launch.mock.calls[0]!;
    callbacks.onOpen?.(shell("terminal_zsh_s2"));
    expect(read()).toEqual({
      layout: { axis: "columns", ratio: 0.5, children: [AGENT, "terminal:terminal_zsh_s2"] },
      focusedKey: "terminal:terminal_zsh_s2",
    });
    // Failing again, the slot says why under its new key.
    callbacks.onError?.(new Error("Still no runner."), shell("terminal_zsh_s2"));
    expect(paneStartFailure(target.conversationId, "terminal:terminal_zsh_s2")?.reason).toBe(
      "Still no runner.",
    );
  });

  it("asks no terminal to take the keyboard when the retry fails again", () => {
    const { target } = memoryTarget("session-retry-fails");
    recordPaneStartFailure(target.conversationId, FAILED, { terminal: "zsh", reason: "x" });
    const launch = vi.fn<(terminal: string, callbacks: TerminalLaunchCallbacks) => void>();
    retryPaneStart(target, FAILED, launch);
    const [, callbacks] = launch.mock.calls[0]!;
    const retried = shell("terminal_zsh_s2");
    setTerminalLaunching(retried.id, "attach", target.conversationId);
    try {
      callbacks.onOpen?.(retried);
      // The create fails: the slot says why, then the launch settles.
      callbacks.onError?.(new Error("Still no runner."), retried);
    } finally {
      setTerminalLaunching(retried.id, null);
    }
    expect(isFailedPaneStart(target.conversationId, "terminal:terminal_zsh_s2")).toBe(true);
    expect(pendingTerminalFocus(target.conversationId, "main")).toBeNull();
  });

  it("gives the keyboard to a shell the server names differently, from the slot's old pane", () => {
    const { target, read } = memoryTarget("session-retry-renamed");
    recordPaneStartFailure(target.conversationId, FAILED, { terminal: "zsh", reason: "x" });
    const launch = vi.fn<(terminal: string, callbacks: TerminalLaunchCallbacks) => void>();
    retryPaneStart(target, FAILED, launch);
    const [, callbacks] = launch.mock.calls[0]!;
    const guessed = shell("terminal_zsh_s2");
    setTerminalLaunching(guessed.id, "attach", target.conversationId);
    // The panes haven't rendered the server's key yet: the keyboard is in the guessed pane.
    const pane = document.createElement("div");
    pane.dataset.terminalPane = "terminal:terminal_zsh_s2";
    pane.innerHTML = `<div ${TERMINAL_SURFACE_ATTRIBUTE}><textarea></textarea></div>`;
    document.body.append(pane);
    try {
      callbacks.onOpen?.(guessed);
      pane.querySelector("textarea")!.focus();
      callbacks.onOpen?.(shell("terminal_zsh_s3"));
    } finally {
      setTerminalLaunching(guessed.id, null);
      pane.remove();
    }
    expect(read()?.focusedKey).toBe("terminal:terminal_zsh_s3");
    expect(pendingTerminalFocus(target.conversationId, "main")?.tabKey).toBe(
      "terminal:terminal_zsh_s3",
    );
  });

  it.each([
    ["asks the slot's new shell to take the keyboard once it starts", false],
    ["leaves the keyboard alone if the person moved it while the shell started", true],
  ])("completing with the id it guessed, the retry %s", (_case, focusMoved) => {
    const conversationId = `session-retry-same-id-${focusMoved}`;
    const { target, read } = memoryTarget(conversationId);
    recordPaneStartFailure(conversationId, FAILED, { terminal: "zsh", reason: "x" });
    const launch = vi.fn<(terminal: string, callbacks: TerminalLaunchCallbacks) => void>();
    retryPaneStart(target, FAILED, launch);
    const [, callbacks] = launch.mock.calls[0]!;
    const retried = shell("terminal_zsh_s2");
    const elsewhere = document.body.appendChild(document.createElement("input"));
    setTerminalLaunching(retried.id, "attach", conversationId);
    try {
      // The server keeps the guessed id, so the launch opens the slot only once.
      callbacks.onOpen?.(retried);
      expect(pendingTerminalFocus(conversationId, "main")).toBeNull();
      if (focusMoved) elsewhere.focus();
    } finally {
      setTerminalLaunching(retried.id, null);
      elsewhere.remove();
    }
    expect(read()?.focusedKey).toBe("terminal:terminal_zsh_s2");
    expect(pendingTerminalFocus(conversationId, "main")?.tabKey ?? null).toBe(
      focusMoved ? null : "terminal:terminal_zsh_s2",
    );
  });

  it("waits while another shell is starting in the session, keeping its error", () => {
    const { target } = memoryTarget("session-busy");
    recordPaneStartFailure(target.conversationId, FAILED, { terminal: "zsh", reason: "x" });
    setTerminalLaunching("terminal_bash_s9", "wait", target.conversationId);
    const launch = vi.fn();
    try {
      expect(retryPaneStart(target, FAILED, launch)).toBe(false);
    } finally {
      setTerminalLaunching("terminal_bash_s9", null);
    }
    expect(launch).not.toHaveBeenCalled();
    expect(isFailedPaneStart(target.conversationId, FAILED)).toBe(true);
  });
});

/** A split of two panes: a failed slot with Try Again, and a live terminal beside it. */
function renderSplit(failedPaneAttributes: Record<string, string> = {}) {
  document.body.innerHTML = `
    <div data-testid="split">
      <div data-terminal-pane="failed">
        <button data-testid="try-again">Try Again</button>
      </div>
      <div data-terminal-pane="live">
        <div ${TERMINAL_SURFACE_ATTRIBUTE}><textarea data-testid="live-input"></textarea></div>
        <input data-testid="find" />
      </div>
    </div>
    <div data-terminal-pane="other-split">
      <div ${TERMINAL_SURFACE_ATTRIBUTE}><textarea data-testid="other-input"></textarea></div>
    </div>
    <textarea data-testid="composer"></textarea>
    <div role="menu"><button data-testid="menu-item">Rename</button></div>`;
  const failed = document.querySelector<HTMLElement>("[data-terminal-pane='failed']")!;
  for (const [name, value] of Object.entries(failedPaneAttributes)) {
    failed.setAttribute(name, value);
  }
  const byTestId = (id: string) => document.querySelector<HTMLElement>(`[data-testid='${id}']`)!;
  return { tryAgain: byTestId("try-again"), byTestId };
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("failedPaneMayTakeFocus", () => {
  it("takes the keyboard from nowhere, its own pane, or a terminal in the same split", () => {
    const { tryAgain, byTestId } = renderSplit();
    (document.activeElement as HTMLElement | null)?.blur();
    expect(failedPaneMayTakeFocus(tryAgain)).toBe(true);
    tryAgain.focus();
    expect(failedPaneMayTakeFocus(tryAgain)).toBe(true);
    byTestId("live-input").focus();
    expect(failedPaneMayTakeFocus(tryAgain)).toBe(true);
  });

  it.each(["find", "composer", "menu-item", "other-input"])(
    "leaves the keyboard where it is when it's in %s",
    (elsewhere) => {
      const { tryAgain, byTestId } = renderSplit();
      byTestId(elsewhere).focus();
      expect(failedPaneMayTakeFocus(tryAgain)).toBe(false);
    },
  );

  it.each<Record<string, string>>([{ "data-pane-hidden": "" }, { "aria-hidden": "true" }])(
    "never takes it while the slot is hidden (%o)",
    (attributes) => {
      const { tryAgain } = renderSplit(attributes);
      (document.activeElement as HTMLElement | null)?.blur();
      expect(failedPaneMayTakeFocus(tryAgain)).toBe(false);
    },
  );
});

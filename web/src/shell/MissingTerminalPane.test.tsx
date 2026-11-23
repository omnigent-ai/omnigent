import type * as UseTerminalsModule from "@/hooks/useTerminals";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TerminalSplitState } from "@/lib/paneSplitLayout";
import { setTerminalLaunching } from "@/lib/terminals";

import { MissingTerminalPane } from "./MissingTerminalPane";
import {
  isFailedPaneStart,
  recordPaneStartFailure,
  resetPaneStartFailuresForTests,
} from "./paneStartFailures";
import type { TerminalSplitTarget } from "./terminalSplitTarget";

const launch = vi.hoisted(() => vi.fn());

vi.mock("@/hooks/useAgents", () => ({
  useSessionAgent: () => ({ data: { terminals: ["zsh"] } }),
}));
vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof UseTerminalsModule>()),
  useCreateTerminal: () => ({ launch, isPending: false }),
  useDeleteTerminal: () => ({ mutate: vi.fn(), isPending: false }),
}));

const AGENT = "terminal:terminal_claude_main";
const FAILED = "terminal:terminal_zsh_s1";

function target(): TerminalSplitTarget {
  let state: TerminalSplitState | undefined = {
    layout: { axis: "columns", ratio: 0.5, children: [AGENT, FAILED] },
    focusedKey: AGENT,
  };
  return {
    id: "main:session-missing",
    conversationId: "session-missing",
    read: () => state,
    update: (update) => {
      state = update(state);
    },
    unsplitKey: () => AGENT,
    canClosePane: (key) => key !== AGENT,
    focusSurface: "main",
  };
}

const renderPane = (split: TerminalSplitTarget, readOnly = false) =>
  render(
    <MissingTerminalPane
      target={split}
      paneKey={FAILED}
      focused={false}
      terminals={[]}
      readOnly={readOnly}
      fallback="Terminal unavailable."
    />,
  );

afterEach(() => {
  cleanup();
  resetPaneStartFailuresForTests();
  launch.mockReset();
});

describe("MissingTerminalPane", () => {
  it("shows the view's message for a pane that isn't a failed start", () => {
    renderPane(target());
    expect(screen.getByText("Terminal unavailable.")).toBeInTheDocument();
  });

  it("gives Try Again the keyboard once another shell has finished starting", () => {
    const split = target();
    recordPaneStartFailure(split.conversationId, FAILED, { terminal: "zsh", reason: "No runner." });
    setTerminalLaunching("terminal_bash_s9", "wait", split.conversationId);
    try {
      render(
        <div data-terminal-pane={FAILED}>
          <MissingTerminalPane
            target={split}
            paneKey={FAILED}
            focused
            terminals={[]}
            readOnly={false}
            fallback="Terminal unavailable."
          />
        </div>,
      );
      const tryAgain = screen.getByRole("button", { name: "Try Again" });
      expect(tryAgain).toBeDisabled();
      expect(document.activeElement).not.toBe(tryAgain);
    } finally {
      act(() => setTerminalLaunching("terminal_bash_s9", null));
    }
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Try Again" }));
  });

  it("says why a shell didn't start, and tries again or closes the pane", () => {
    const split = target();
    recordPaneStartFailure(split.conversationId, FAILED, { terminal: "zsh", reason: "No runner." });
    renderPane(split);
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't start the shell.No runner.");
    fireEvent.click(screen.getByRole("button", { name: "Try Again" }));
    expect(launch).toHaveBeenCalledWith("zsh", expect.any(Object));

    // Trying again cleared the failure; the launch failing again brings it back.
    act(() =>
      recordPaneStartFailure(split.conversationId, FAILED, {
        terminal: "zsh",
        reason: "No runner.",
      }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Close Pane" }));
    expect(isFailedPaneStart(split.conversationId, FAILED)).toBe(false);
    expect(split.read()).toBeUndefined();
  });

  it("offers neither button to a viewer who can't change the session", () => {
    const split = target();
    recordPaneStartFailure(split.conversationId, FAILED, { terminal: "zsh", reason: "No runner." });
    renderPane(split, true);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

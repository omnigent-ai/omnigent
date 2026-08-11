// BlockRenderer dispatch wiring. The kind→component switch is easy
// to break by removing a case — neither the walker nor the
// individual card tests catch that. Drive it directly here.

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RenderItem } from "@/lib/renderItems";
import { ConversationScrollLockContext } from "@/components/ai-elements/conversation";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { normalizeExplicitMathDelimiters } from "@/components/ai-elements/mathMarkdown";
import { BlockRenderer } from "./BlockRenderer";

afterEach(cleanup);

// Stick-to-bottom lock fixture for the fold's expand snap. Module scope so
// the provider value is a constant (jsx-no-constructed-context-values);
// reset inside the test that uses it.
const lockState = { isAtBottom: true, escapedFromLock: false };
const lockStopScroll = vi.fn();
const lockValue = { stopScroll: lockStopScroll, state: lockState };

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};

const renderMarkdownText = (text: string) =>
  render(
    <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
      <BlockRenderer
        items={[{ kind: "text", itemId: "t1", text, final: true }]}
        sessionStatus="idle"
      />
    </FileViewerContext.Provider>,
  );

describe("BlockRenderer dispatch", () => {
  it("renders a slash_command RenderItem via SlashCommandCard", () => {
    const items: RenderItem[] = [
      {
        kind: "slash_command",
        itemId: "sc_1",
        slashKind: "skill",
        name: "dev-productivity:simplify",
        arguments: "",
        output: null,
      },
    ];
    render(<BlockRenderer items={items} sessionStatus="idle" />);
    expect(screen.getByText("Skill")).toBeDefined();
    expect(screen.getByText("dev-productivity:simplify")).toBeDefined();
  });

  it("passes slashKind='command' through to the card prefix", () => {
    // Guards against the dispatch dropping the slashKind→kind prop;
    // removing the kind={item.slashKind} prop on the card would make
    // SlashCommandCard's destructure throw at render time.
    const items: RenderItem[] = [
      {
        kind: "slash_command",
        itemId: "sc_2",
        slashKind: "command",
        name: "effort",
        arguments: "high",
        output: null,
      },
    ];
    render(<BlockRenderer items={items} sessionStatus="idle" />);
    expect(screen.getByText("Command")).toBeDefined();
    expect(screen.getByText("effort")).toBeDefined();
  });

  it("renders a terminal_command input RenderItem via TerminalCommandCard", () => {
    const items: RenderItem[] = [
      {
        kind: "terminal_command",
        itemId: "tc_1",
        terminalKind: "input",
        input: "pwd",
        stdout: null,
        stderr: null,
      },
    ];
    render(<BlockRenderer items={items} sessionStatus="idle" />);
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-kind")).toBe("input");
    expect(screen.getByText("pwd")).toBeDefined();
  });

  it("renders a terminal_command output RenderItem via TerminalCommandCard", () => {
    const items: RenderItem[] = [
      {
        kind: "terminal_command",
        itemId: "tc_2",
        terminalKind: "output",
        input: null,
        stdout: "/home/user",
        stderr: "",
      },
    ];
    render(<BlockRenderer items={items} sessionStatus="idle" />);
    const card = screen.getByTestId("terminal-command-card");
    expect(card.getAttribute("data-terminal-kind")).toBe("output");
  });

  it("renders error diagnostics with local wrapping and preserved line breaks", () => {
    const message = [
      "Required terminal exited unexpectedly; the session runtime is no longer available.",
      "Lifecycle diagnostics:",
      "terminal: required-runtime:main",
      "command: runtime-worker (10 args; argv omitted because terminal args may contain secrets)",
      "cwd: /workspace/project",
      "last captured output:",
      "  - first diagnostic line",
      "  - second diagnostic line",
    ].join("\n");
    const items: RenderItem[] = [
      {
        kind: "error",
        itemId: null,
        source: "",
        code: "required_terminal_exited",
        message,
      },
    ];

    const { container } = render(<BlockRenderer items={items} sessionStatus="idle" />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveClass("min-w-0");
    expect(alert).toHaveClass("overflow-hidden");

    const description = container.querySelector('[data-slot="alert-description"]');
    expect(description).not.toBeNull();
    expect(description).toHaveClass("min-w-0");
    expect(description).toHaveClass("overflow-hidden");

    const messageNode = screen.getByText(/Required terminal exited unexpectedly/);
    expect(messageNode).toHaveClass("whitespace-pre-wrap");
    expect(messageNode).toHaveClass("break-words");
    expect(messageNode.textContent).toContain(
      "Lifecycle diagnostics:\nterminal: required-runtime:main",
    );
    expect(messageNode.textContent).toContain(
      "  - first diagnostic line\n  - second diagnostic line",
    );
  });

  it("renders a friendly failure card when the error is classified", () => {
    const items: RenderItem[] = [
      {
        kind: "error",
        itemId: null,
        source: "",
        code: "required_terminal_exited",
        title: "Claude Code can't run as root",
        cause:
          "The agent terminal exited immediately because Claude Code refuses the flag as root.",
        remediation: "Run the host as a non-root user (uid != 0).",
        message: "Claude Code can't run as root\n\nTerminal diagnostics:\ncommand: claude",
      },
    ];

    render(<BlockRenderer items={items} sessionStatus="idle" />);

    // Headline is the friendly title, not the raw code.
    expect(screen.getByText("Claude Code can't run as root")).toBeDefined();
    // Cause is shown in plain English.
    expect(screen.getByText(/refuses the flag as root/)).toBeDefined();
    // Remediation is surfaced.
    expect(screen.getByText(/Run the host as a non-root user/)).toBeDefined();
    // Raw diagnostics are folded away behind a Details toggle (collapsed).
    expect(screen.getByText(/Details/)).toBeDefined();
    // The raw enum is NOT the visible headline.
    expect(screen.queryByText(/Error · required_terminal_exited/)).toBeNull();
  });

  it("falls back to a code→sentence description for an unclassified failure", () => {
    const items: RenderItem[] = [
      {
        kind: "error",
        itemId: null,
        source: "",
        code: "runner_error",
        message: "",
      },
    ];

    render(<BlockRenderer items={items} sessionStatus="idle" />);

    // Even with an empty message, the known code reads as an English headline
    // instead of the raw enum.
    expect(screen.getByText("Something went wrong setting up the turn on the host.")).toBeDefined();
    expect(screen.queryByText(/runner_error/)).toBeNull();
  });

  it("treats a trailing reasoning item as streaming when sessionStatus is running", () => {
    const items: RenderItem[] = [
      { kind: "reasoning", itemId: null, text: "thinking", duration: undefined },
    ];
    render(<BlockRenderer items={items} sessionStatus="running" />);
    expect(screen.getByText("Thinking...")).toBeDefined();
  });

  it("does NOT treat a reasoning item as streaming when sessionStatus is idle", () => {
    const items: RenderItem[] = [
      { kind: "reasoning", itemId: null, text: "thinking", duration: undefined },
    ];
    render(<BlockRenderer items={items} sessionStatus="idle" />);
    expect(screen.queryByText("Thinking...")).toBeNull();
  });

  it("does NOT treat reasoning as streaming once a text item follows it", () => {
    const items: RenderItem[] = [
      { kind: "reasoning", itemId: null, text: "thinking", duration: undefined },
      { kind: "text", itemId: "t1", text: "hello", final: false },
    ];
    render(<BlockRenderer items={items} sessionStatus="running" />);
    expect(screen.queryByText("Thinking...")).toBeNull();
  });

  it("adds subtle separation between adjacent assistant text items", async () => {
    const items: RenderItem[] = [
      { kind: "text", itemId: "t1", text: "First message.", final: true },
      { kind: "text", itemId: "t2", text: "Second **markdown** message.", final: true },
    ];

    const { container } = render(
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <BlockRenderer items={items} sessionStatus="idle" />
      </FileViewerContext.Provider>,
    );

    const sections = container.querySelectorAll<HTMLElement>(
      '[data-testid="assistant-text-section"]',
    );
    expect(sections).toHaveLength(2);
    expect(sections[0]!).not.toHaveClass("mt-2");
    expect(sections[1]!).toHaveClass("mt-2");
    expect(screen.getByText("First message.")).toBeDefined();
    expect(screen.getByText(/Second/)).toBeDefined();

    const strong = await screen.findByText("markdown", {
      selector: '[data-streamdown="strong"]',
    });
    expect(sections[1]!.contains(strong)).toBe(true);
  });

  it("does not add adjacent-text spacing across tool items", () => {
    // Rendered as a live turn ("running") so the whole trace stays
    // expanded — a settled turn would fold "Before tool." behind the
    // Worked row and unmount it.
    const items: RenderItem[] = [
      { kind: "text", itemId: "t1", text: "Before tool.", final: true },
      {
        kind: "tool",
        itemId: "fc_1",
        execution: {
          name: "read_file",
          arguments: {},
          argsSummary: "",
          callId: "call_1",
          agentName: "test",
          executedBy: "server",
          output: "ok",
        },
        output: "ok",
        state: "output-available",
        startedAt: null,
        duration: undefined,
      },
      { kind: "text", itemId: "t2", text: "After tool.", final: true },
    ];

    const { container } = render(
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <BlockRenderer items={items} sessionStatus="running" />
      </FileViewerContext.Provider>,
    );

    const sections = container.querySelectorAll<HTMLElement>(
      '[data-testid="assistant-text-section"]',
    );
    expect(sections).toHaveLength(2);
    expect(sections[0]!).not.toHaveClass("mt-2");
    expect(sections[1]!).not.toHaveClass("mt-2");
  });

  it("labels the fold with only the hidden count while the tail streams", () => {
    // The most-recent tools render as a visible tail OUTSIDE the fold, so
    // the fold line describes only what's hidden ("Called 2 tools") — a
    // whole-run count would double-count the tool cards visible below.
    const tool = (n: number): RenderItem => ({
      kind: "tool",
      itemId: `fc_${n}`,
      execution: {
        name: `tool_${n}`,
        arguments: {},
        argsSummary: "",
        callId: `c_${n}`,
        agentName: "nessie",
        executedBy: "server",
        output: "ok",
      },
      output: "ok",
      state: "output-available",
      startedAt: null,
      duration: undefined,
    });
    const items: RenderItem[] = [
      { kind: "text", itemId: "m0", text: "Dispatching.", final: true },
      tool(1),
      tool(2),
      tool(3),
      tool(4),
      tool(5),
    ];
    render(<BlockRenderer items={items} sessionStatus="running" />);
    expect(screen.getByText("Called 2 tools")).toBeDefined();
    expect(screen.queryByText("Called 5 tools")).toBeNull();
    // The recent tools must be visible as a tail OUTSIDE the collapsed
    // group — the most-recent tool renders (the collapsed group's content
    // is unmounted), while an older one stays folded. Guards against a
    // regression that folds everything.
    expect(screen.getByText(/tool_5/)).toBeDefined();
    expect(screen.queryByText(/tool_1/)).toBeNull();
  });

  describe("settled-turn process fold (Worked row)", () => {
    const tool = (n: number, name = `tool_${n}`): RenderItem => ({
      kind: "tool",
      itemId: `fc_${n}`,
      execution: {
        name,
        arguments: {},
        argsSummary: "",
        callId: `c_${n}`,
        agentName: "nessie",
        executedBy: "server",
        output: "ok",
      },
      output: "ok",
      state: "output-available",
      startedAt: null,
      duration: undefined,
    });

    it("folds narration + tool runs behind the Worked row, leaving the answer visible", () => {
      // Codex-desktop demarcation: once the turn settles, the whole
      // process trace collapses behind one "Worked" expander so the
      // final answer is unambiguously where reading starts. Expanding
      // replays the trace with the semantic tool-run labels inside.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Planning the run.", final: true },
        tool(1, "Bash"),
        tool(2, "Bash"),
        tool(3),
        tool(4),
        tool(5),
        { kind: "text", itemId: "m1", text: "All done here.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.getByText("Worked")).toBeDefined();
      // The answer stays visible; the trace (narration + run labels)
      // is unmounted until expanded.
      expect(screen.getByText("All done here.")).toBeDefined();
      expect(screen.queryByText("Planning the run.")).toBeNull();
      expect(screen.queryByText(/Ran 2 shell commands/)).toBeNull();

      fireEvent.click(screen.getByText("Worked"));
      expect(screen.getByText("Planning the run.")).toBeDefined();
      expect(screen.getByText("Ran 2 shell commands, called 3 other tools")).toBeDefined();
      // The answer remains visible after expansion too.
      expect(screen.getByText("All done here.")).toBeDefined();
    });

    it("labels the Worked row with the turn duration when provided", () => {
      const items: RenderItem[] = [
        tool(1),
        { kind: "text", itemId: "m1", text: "Done.", final: true },
      ];
      render(<BlockRenderer items={items} sessionStatus="idle" workedForS={106} />);
      expect(screen.getByText("Worked for 1m 46s")).toBeDefined();
    });

    describe("expand scroll-into-view", () => {
      // jsdom has no scrollIntoView — install a spy so the scroll path is
      // observable (and its absence provable).
      const scrollSpy = vi.fn();
      beforeEach(() => {
        (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView = scrollSpy;
      });
      afterEach(() => {
        scrollSpy.mockReset();
        delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      });

      const settledItems = (): RenderItem[] => [
        tool(1),
        { kind: "text", itemId: "m1", text: "Done.", final: true },
      ];

      it("snaps the row into view on a user expand", () => {
        // Growing the trace never keeps the row put on its own: the
        // stick-to-bottom scroller re-pins the bottom on the last turn,
        // and native scroll anchoring pins the answer elsewhere. Every
        // user expand snaps the row to the top so the trace reads from
        // its beginning.
        render(
          <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
            <BlockRenderer items={settledItems()} sessionStatus="idle" />
          </FileViewerContext.Provider>,
        );
        fireEvent.click(screen.getByText("Worked"));
        expect(screen.getByText("Called 1 tool")).toBeDefined();
        expect(scrollSpy).toHaveBeenCalledTimes(1);
        expect(scrollSpy).toHaveBeenCalledWith({ block: "start" });
      });

      it("releases the stick-to-bottom lock before snapping", () => {
        // Viewing the last turn the view is pinned; without the release
        // the library's resize-driven scrollToBottom overrides the snap
        // (isAtBottom is still true from the pre-click view) and the
        // click appears to do nothing.
        lockState.isAtBottom = true;
        lockState.escapedFromLock = false;
        lockStopScroll.mockReset();
        render(
          <ConversationScrollLockContext.Provider value={lockValue}>
            <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
              <BlockRenderer items={settledItems()} sessionStatus="idle" />
            </FileViewerContext.Provider>
          </ConversationScrollLockContext.Provider>,
        );
        fireEvent.click(screen.getByText("Worked"));
        expect(lockStopScroll).toHaveBeenCalledTimes(1);
        expect(lockState.isAtBottom).toBe(false);
        expect(lockState.escapedFromLock).toBe(true);
        expect(scrollSpy).toHaveBeenCalledWith({ block: "start" });
      });

      it("parks the scroller's scroll anchoring for the expand animation", () => {
        // Even a fits-on-screen expand grows the trace over the 200ms
        // height animation; with anchoring live, the browser pins the
        // answer below and glides the row off the top. The scroller
        // must run with overflow-anchor: none for the hold window.
        vi.useFakeTimers();
        try {
          render(
            <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
              <div style={{ overflowY: "auto" }} data-testid="scroller">
                <BlockRenderer items={settledItems()} sessionStatus="idle" />
              </div>
            </FileViewerContext.Provider>,
          );
          const scroller = screen.getByTestId("scroller");
          fireEvent.click(screen.getByText("Worked"));
          expect(scroller.style.overflowAnchor).toBe("none");
          act(() => {
            vi.advanceTimersByTime(400);
          });
          expect(scroller.style.overflowAnchor).toBe("");
        } finally {
          vi.useRealTimers();
        }
      });

      it("never scrolls on the animateCollapse mount-close cycle", async () => {
        // The fold appearing over a watched settle mounts OPEN and closes
        // a frame later — programmatic, not a user expand; scrolling
        // there would yank the reader away from the answer.
        const { rerender } = render(
          <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
            <BlockRenderer items={settledItems()} sessionStatus="idle" turnLifecycle="streaming" />
          </FileViewerContext.Provider>,
        );
        rerender(
          <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
            <BlockRenderer items={settledItems()} sessionStatus="idle" turnLifecycle="completed" />
          </FileViewerContext.Provider>,
        );
        await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());
        await waitFor(() => expect(screen.queryByText("Called 1 tool")).toBeNull());
        expect(scrollSpy).not.toHaveBeenCalled();
      });
    });

    it("trusts turnLifecycle over sessionStatus for liveness", async () => {
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Looking around.", final: true },
        tool(1),
        { kind: "text", itemId: "m1", text: "Answer text.", final: false },
      ];
      // Streaming turn: stays expanded even though the session status
      // lags (e.g. a stale snapshot said idle).
      const { rerender } = render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" turnLifecycle="streaming" />
        </FileViewerContext.Provider>,
      );
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Looking around.")).toBeDefined();

      // Completed turn folds even while the SESSION is still running
      // (a later turn is the live one, not this bubble) — after the
      // settle debounce.
      rerender(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="running" turnLifecycle="completed" />
        </FileViewerContext.Provider>,
      );
      await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());
      // The trace collapses a frame later (it mounts open to animate away).
      await waitFor(() => expect(screen.queryByText("Looking around.")).toBeNull());
      expect(screen.getByText("Answer text.")).toBeDefined();
    });

    it("keeps persistent dispatch cards visible outside the fold", () => {
      const dispatch: RenderItem = {
        kind: "tool",
        itemId: "fc_send",
        execution: {
          name: "sys_session_send",
          arguments: { agent: "deep_thought", title: "q1" },
          argsSummary: "",
          callId: "c_send",
          agentName: "nessie",
          executedBy: "server",
          output: "ok",
        },
        output: "ok",
        state: "output-available",
        startedAt: null,
        duration: undefined,
      };
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Dispatching.", final: true },
        dispatch,
        tool(1),
        { kind: "text", itemId: "m1", text: "Relayed.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      // The dispatch card renders outside the fold; the plain tool and
      // the narration stay hidden inside it.
      expect(screen.getByText(/sys_session_send/)).toBeDefined();
      expect(screen.queryByText("Dispatching.")).toBeNull();
      expect(screen.queryByText(/tool_1/)).toBeNull();
      expect(screen.getByText("Relayed.")).toBeDefined();
    });

    const elicitation = (
      status: "pending" | "responded",
      response: { action: "accept" } | null,
    ): RenderItem => ({
      kind: "elicitation",
      itemId: "el_1",
      elicitationId: "elc_1",
      message: "Claude wants to call Bash",
      phase: "pre_tool",
      policyName: "claude_native_permission",
      contentPreview: "",
      requestedSchema: {},
      status,
      response,
    });

    it("folds resolved approval cards inside the trace", () => {
      // A responded approval is part of the work's history — it folds
      // in document order with the rest of the trace instead of
      // dangling between the Worked row and the answer.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Requesting approval.", final: true },
        elicitation("responded", { action: "accept" }),
        tool(1),
        { kind: "text", itemId: "m1", text: "All done.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.queryByTestId("approval-card")).toBeNull();
      expect(screen.getByText("All done.")).toBeDefined();

      fireEvent.click(screen.getByText("Worked"));
      const card = screen.getByTestId("approval-card");
      expect(card.getAttribute("data-state")).toBe("responded");
      expect(screen.getByText("Requesting approval.")).toBeDefined();
    });

    it("keeps a pending elicitation visible outside the fold", () => {
      // Defensive: ChatPage normally floats pending cards to the page
      // bottom, but if one reaches the renderer it must never be
      // hidden behind a click — it is actionable.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Requesting approval.", final: true },
        elicitation("pending", null),
        tool(1),
        { kind: "text", itemId: "m1", text: "All done.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.getByTestId("approval-card").getAttribute("data-state")).toBe("pending");
      expect(screen.queryByText("Requesting approval.")).toBeNull();
    });

    it("does not fold a turn with no trailing answer", () => {
      // Interrupted / tool-only turns have nothing to demarcate — the
      // trace stays visible (runs still fold to their summary rows).
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Starting work.", final: true },
        tool(1),
        tool(2),
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Starting work.")).toBeDefined();
      expect(screen.getByText("Called 2 tools")).toBeDefined();
    });

    it("folds trailing bookkeeping tools (turn_diff) instead of blocking the fold", () => {
      // codex-native appends a `turn_diff` mirror AFTER the final
      // message; the answer detection must look past it so codex
      // turns still demarcate, and the diff folds as process.
      const turnDiff: RenderItem = {
        kind: "tool",
        itemId: "fc_diff",
        execution: {
          name: "turn_diff",
          arguments: {},
          argsSummary: "",
          callId: "c_diff",
          agentName: "codex",
          executedBy: "server",
          output: "diff",
        },
        output: "diff",
        state: "output-available",
        startedAt: null,
        duration: undefined,
      };
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Editing the file.", final: true },
        tool(1, "shell"),
        { kind: "text", itemId: "m1", text: "Edit landed.", final: true },
        turnDiff,
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.getByText("Edit landed.")).toBeDefined();
      expect(screen.queryByText("Editing the file.")).toBeNull();
      expect(screen.queryByText(/turn_diff/)).toBeNull();

      // Expanding the Worked row reveals the trace; the diff sits in
      // the (still-collapsed) tool-run group alongside the shell call.
      fireEvent.click(screen.getByText("Worked"));
      expect(screen.getByText("Editing the file.")).toBeDefined();
      const runLabel = screen.getByText("Ran 1 shell command, called 1 other tool");
      fireEvent.click(runLabel);
      expect(screen.getByText(/turn_diff/)).toBeDefined();
    });

    it("folds a continued turn that yielded before answering", () => {
      // Dispatching sub-agents ends the turn mid-task: this bubble holds
      // narration + tool calls and NO answer (that lands in the next
      // assistant bubble). Without the `continued` flag it stayed fully
      // expanded — the wall of narration the fold exists to remove.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Dispatching two sub-agents.", final: true },
        tool(1, "Agent"),
        tool(2, "Agent"),
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" continued workedForS={42} />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByText("Worked for 42s")).toBeDefined();
      expect(screen.queryByText("Dispatching two sub-agents.")).toBeNull();

      fireEvent.click(screen.getByText("Worked for 42s"));
      expect(screen.getByText("Dispatching two sub-agents.")).toBeDefined();
    });

    it("never folds the last assistant bubble while the session is running", async () => {
      // A mid-turn (re)connect can miss the edge that names the turn, so
      // the LIVE turn's lifecycle reads "completed" — folding it made the
      // trace collapse and reopen as its tail alternated between text and
      // tools (the codex flicker). While the session runs, the last
      // bubble stays expanded; the terminal status edge folds it.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Checking the CLI.", final: true },
        tool(1, "Bash"),
        { kind: "text", itemId: "m1", text: "Found it.", final: true },
      ];
      const view = (status: "running" | "idle") => (
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={items}
            sessionStatus={status}
            turnLifecycle="completed"
            isLastAssistant
          />
        </FileViewerContext.Provider>
      );
      const { rerender } = render(view("running"));
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Checking the CLI.")).toBeDefined();

      // The session's terminal edge lands → the fold forms (after the
      // settle debounce).
      rerender(view("idle"));
      await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());
      await waitFor(() => expect(screen.queryByText("Checking the CLI.")).toBeNull());
    });

    it("keeps a shown fold through a scheduled wake's running edge", async () => {
      // A /loop iteration ends, folds, and minutes later a cron/wakeup
      // firing flips the session to running while this settled bubble is
      // still the last one (the new turn has no items yet). The shown
      // fold must hold through that item-less gap instead of popping
      // open every iteration.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Polling CI.", final: true },
        tool(1, "Bash"),
        { kind: "text", itemId: "m1", text: "All green this round.", final: true },
      ];
      const view = (status: "running" | "idle") => (
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={items}
            sessionStatus={status}
            turnLifecycle="completed"
            isLastAssistant
            showsWorking={status === "running"}
          />
        </FileViewerContext.Provider>
      );
      const { rerender } = render(view("idle"));
      await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());

      rerender(view("running"));
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      // Structural, not timing: the fold stays across further renders.
      rerender(view("running"));
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.queryByText("Polling CI.")).toBeNull();
    });

    it("a revive clears the latch and restores live-turn suppression", async () => {
      // If this bubble's OWN turn goes live again (a stray idle's
      // revive), the trace re-expands, and a later mid-turn settled
      // misread goes back to being suppressed — the latch must not
      // carry across a revive and resurrect the codex flicker.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Working through it.", final: true },
        tool(1, "Bash"),
        { kind: "text", itemId: "m1", text: "Done for now.", final: true },
      ];
      const view = (lifecycle: "completed" | "streaming", status: "running" | "idle") => (
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={items}
            sessionStatus={status}
            turnLifecycle={lifecycle}
            isLastAssistant
          />
        </FileViewerContext.Provider>
      );
      const { rerender } = render(view("completed", "idle"));
      await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());

      // The turn revives — trace expands immediately.
      rerender(view("streaming", "running"));
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Working through it.")).toBeDefined();

      // A settled misread while the session still runs stays suppressed.
      rerender(view("completed", "running"));
      await new Promise((resolve) => {
        setTimeout(resolve, 700);
      });
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
    });

    it("holds the mount fold over a just-active trace so a live edge can cancel it", () => {
      // A reload can land inside a step-wise turn's between-step gap,
      // where the snapshot reads settled although the turn continues.
      // Folding instantly then unfolding on the next step's running edge
      // was the reported flash — a recent trace waits out the longer
      // mount debounce instead.
      vi.useFakeTimers();
      try {
        const items: RenderItem[] = [
          { kind: "text", itemId: "m0", text: "Checking the CLI.", final: true },
          tool(1, "Bash"),
          { kind: "text", itemId: "m1", text: "Approved; continuing.", final: true },
        ];
        const view = (status: "idle" | "running") => (
          <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
            <BlockRenderer
              items={items}
              sessionStatus={status}
              turnLifecycle="completed"
              isLastAssistant
              lastActivityAtS={Date.now() / 1000 - 2}
            />
          </FileViewerContext.Provider>
        );
        const { rerender } = render(view("idle"));
        // No instant fold, and none 1s in — the mount debounce is holding.
        expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
        act(() => vi.advanceTimersByTime(1_000));
        expect(screen.queryByTestId("turn-worked-fold")).toBeNull();

        // The next step's running edge lands → the fold is cancelled.
        rerender(view("running"));
        act(() => vi.advanceTimersByTime(5_000));
        expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
        expect(screen.getByText("Checking the CLI.")).toBeDefined();
      } finally {
        vi.useRealTimers();
      }
    });

    it("folds a just-active trace after the mount debounce when nothing follows", () => {
      vi.useFakeTimers();
      try {
        const items: RenderItem[] = [
          { kind: "text", itemId: "m0", text: "Narration.", final: true },
          tool(1),
          { kind: "text", itemId: "m1", text: "The answer.", final: true },
        ];
        render(
          <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
            <BlockRenderer
              items={items}
              sessionStatus="idle"
              turnLifecycle="completed"
              isLastAssistant
              lastActivityAtS={Date.now() / 1000 - 2}
            />
          </FileViewerContext.Provider>,
        );
        expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
        act(() => vi.advanceTimersByTime(3_500));
        expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      } finally {
        vi.useRealTimers();
      }
    });

    it("mounts an OLD settled trace already folded, with no delay", () => {
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Narration.", final: true },
        tool(1),
        { kind: "text", itemId: "m1", text: "The answer.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={items}
            sessionStatus="idle"
            turnLifecycle="completed"
            isLastAssistant
            lastActivityAtS={Date.now() / 1000 - 3600}
          />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.queryByText("Narration.")).toBeNull();
    });

    it("never folds the last bubble while an elicitation is parked, even if all else reads settled", async () => {
      // Reload while parked on an approval: a step-wise turn's snapshot
      // names the STEP id (not the items' thread id), so the trace's
      // lifecycle reads "completed" AND the session status can read
      // settled — but the pending card proves the turn is still in
      // flight. Folding here collapsed the partial work into a
      // premature "Worked for" row.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Checking the CLI.", final: true },
        tool(1, "Bash"),
        { kind: "text", itemId: "m1", text: "Need approval next.", final: true },
      ];
      const view = (parked: boolean) => (
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={items}
            sessionStatus="idle"
            turnLifecycle="completed"
            isLastAssistant
            hasPendingElicitation={parked}
          />
        </FileViewerContext.Provider>
      );
      const { rerender } = render(view(true));
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Checking the CLI.")).toBeDefined();

      // Card answered and the turn settles → the fold forms.
      rerender(view(false));
      await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());
    });

    it("folds an earlier (non-last) settled bubble even while the session runs", () => {
      // Only the LAST bubble can be the live turn; prior turns fold as
      // usual while a later turn streams.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Old narration.", final: true },
        tool(1),
        { kind: "text", itemId: "m1", text: "Old answer.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={items}
            sessionStatus="running"
            turnLifecycle="completed"
            isLastAssistant={false}
          />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
    });

    it("folds a turn whose reasoning burst lands after the answer", () => {
      // Codex opens a reasoning section as the turn ends, so the item
      // arrives AFTER the final message. Reasoning is process, never the
      // answer — without peeling it the turn stayed expanded live while a
      // reload (where the transient item is absent) folded the same turn.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Checking the CLI.", final: true },
        tool(1, "Bash"),
        { kind: "text", itemId: "m1", text: "Server started on 8838.", final: true },
        { kind: "reasoning", itemId: null, text: "", duration: undefined },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" turnLifecycle="completed" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      // The answer stays out; the narration folds away with the trace.
      expect(screen.getByText("Server started on 8838.")).toBeDefined();
      expect(screen.queryByText("Checking the CLI.")).toBeNull();
    });

    it("never folds a bubble made only of streaming artifacts", () => {
      // Codex splits an in-flight turn into fragments: a reasoning burst
      // (no item id yet) plus a `live:` narration preview. Their
      // synthetic response id never matches activeResponse, so the
      // walker labels them "completed" and this one folded mid-turn —
      // then the fold vanished when the authoritative item replaced the
      // preview. That flicker is what oscillated on screen.
      const items: RenderItem[] = [
        { kind: "reasoning", itemId: null, text: "Planning server startup", duration: 1 },
        { kind: "text", itemId: "live:msg_1", text: "I'll check the CLI shape.", final: false },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="running" turnLifecycle="completed" />
        </FileViewerContext.Provider>,
      );
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("I'll check the CLI shape.")).toBeDefined();
    });

    it("leaves a continued fragment that ran nothing expanded", () => {
      // Streaming splits a turn into fragments (a reasoning burst, a
      // narration preview) that are `continued` by the rest of the turn.
      // Folding those produced a lone "Worked" row with nothing behind
      // it — and they flipped folded/unfolded as fragments merged away.
      const items: RenderItem[] = [
        { kind: "reasoning", itemId: "r0", text: "Thinking it over.", duration: 1 },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" continued />
        </FileViewerContext.Provider>,
      );
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
    });

    it("still leaves an unanswered, uncontinued turn expanded", () => {
      // Nothing continues it, so folding would hide the turn's only
      // content behind a click with no answer to demarcate.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Dispatching two sub-agents.", final: true },
        tool(1, "Agent"),
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" continued={false} />
        </FileViewerContext.Provider>,
      );
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Dispatching two sub-agents.")).toBeDefined();
    });

    it("mounts the fold OPEN when the turn settles on screen, then closes it", async () => {
      // The smooth-collapse contract: a turn that settles while the user
      // watches keeps its trace mounted for one frame so it can animate
      // into the row, instead of a tall block vanishing instantly.
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Looking around.", final: true },
        tool(1),
        { kind: "text", itemId: "m1", text: "All done.", final: true },
      ];
      const view = (lifecycle: "streaming" | "completed") => (
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="running" turnLifecycle={lifecycle} />
        </FileViewerContext.Provider>
      );
      const { rerender } = render(view("streaming"));
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();

      rerender(view("completed"));
      // The fold waits out the settle debounce (absorbing transient
      // settled reads), then appears — trace still on screen until it
      // mounts open and closes itself.
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Looking around.")).toBeDefined();
      await waitFor(() => expect(screen.getByTestId("turn-worked-fold")).toBeDefined());
      await waitFor(() => expect(screen.queryByText("Looking around.")).toBeNull());
      expect(screen.getByText("All done.")).toBeDefined();
    });

    it("mounts settled history already collapsed (nothing to animate)", () => {
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Looking around.", final: true },
        tool(1),
        { kind: "text", itemId: "m1", text: "All done.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" turnLifecycle="completed" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.queryByText("Looking around.")).toBeNull();
    });

    it("does not fold an all-text turn", () => {
      const items: RenderItem[] = [
        { kind: "text", itemId: "m0", text: "Just an answer.", final: true },
      ];
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="idle" />
        </FileViewerContext.Provider>,
      );
      expect(screen.queryByTestId("turn-worked-fold")).toBeNull();
      expect(screen.getByText("Just an answer.")).toBeDefined();
    });
  });

  describe("math rendering", () => {
    it("normalizes explicit TeX delimiters outside code", () => {
      expect(normalizeExplicitMathDelimiters(String.raw`中文 \(\sqrt{x}\) 文本`)).toBe(
        String.raw`中文 $\sqrt{x}$ 文本`,
      );
      expect(normalizeExplicitMathDelimiters(String.raw`\[\sqrt{x}\]`)).toBe(
        String.raw`$$\sqrt{x}$$`,
      );
      expect(normalizeExplicitMathDelimiters(String.raw`\`\(\sqrt{x}\)\``)).toBe(
        String.raw`\`\(\sqrt{x}\)\``,
      );
      expect(
        normalizeExplicitMathDelimiters(["```", String.raw`\(\sqrt{x}\)`, "```"].join("\n")),
      ).toBe(["```", String.raw`\(\sqrt{x}\)`, "```"].join("\n"));
    });

    it("leaves LaTeX line breaks inside existing display math untouched", () => {
      // `\\[1em]` is a spaced line break, not an explicit display-math opener;
      // converting its `\[` to `$$` would corrupt the already-`$$`-delimited block.
      const aligned = String.raw`$$\begin{aligned} a &= b \\[1em] c &= d \end{aligned}$$`;
      expect(normalizeExplicitMathDelimiters(aligned)).toBe(aligned);
    });

    it("does not convert delimiters already inside a dollar-math span", () => {
      const inline = String.raw`$\[x\]$`;
      expect(normalizeExplicitMathDelimiters(inline)).toBe(inline);
    });

    it("skips normalization inside multi-backtick inline code", () => {
      const doubleTick = "``" + String.raw`\(x\)` + "``";
      expect(normalizeExplicitMathDelimiters(doubleTick)).toBe(doubleTick);
    });

    it("escapes currency dollar amounts so prose isn't parsed as inline math", () => {
      // With single-dollar math enabled, "it costs $5 or $10" would otherwise
      // parse "5 or " as inline math. Escaping the digit-led `$` renders literal
      // dollar figures and stops the run from flipping the math toggle.
      expect(normalizeExplicitMathDelimiters("it costs $5 or $10")).toBe(
        String.raw`it costs \$5 or \$10`,
      );
      // Delimiters after the currency text still normalize — the toggle didn't flip.
      expect(normalizeExplicitMathDelimiters(String.raw`$5 then \(x\)`)).toBe(
        String.raw`\$5 then $x$`,
      );
    });

    it("does not double-escape an already-escaped dollar", () => {
      expect(normalizeExplicitMathDelimiters(String.raw`\$5`)).toBe(String.raw`\$5`);
    });

    it("detects indented code fences per CommonMark", () => {
      const indentedFence = ["   ```", String.raw`\(\sqrt{x}\)`, "   ```"].join("\n");
      expect(normalizeExplicitMathDelimiters(indentedFence)).toBe(indentedFence);
    });

    it("keeps a mismatched fence marker inside a code block as literal text", () => {
      // A `~~~` line inside a ``` block does not close it (CommonMark requires
      // the same fence char), so delimiters there must stay verbatim.
      const block = ["```", "~~~", String.raw`\(\sqrt{x}\)`, "```"].join("\n");
      expect(normalizeExplicitMathDelimiters(block)).toBe(block);
    });

    it("loads required Streamdown and KaTeX styles at the app entrypoint", () => {
      // KaTeX's DOM is mostly positioned spans. Without its stylesheet, radicals
      // and fractions can leave only the root bar/outer shell visible while the
      // radicand appears missing. Keep this as a source-level guard because
      // jsdom cannot catch visual CSS layout failures. Resolve relative to this
      // file (not process.cwd()) so the test is independent of the runner's dir.
      const srcDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
      const entrypoints = ["main.tsx", "embed.tsx"].map((file) =>
        readFileSync(path.join(srcDir, file), "utf8"),
      );
      const indexCss = readFileSync(path.join(srcDir, "index.css"), "utf8");

      for (const source of entrypoints) {
        expect(source).toContain('import "katex/dist/katex.min.css"');
        expect(source).toContain('import "streamdown/styles.css"');
      }
      expect(indexCss).toContain('@source "../node_modules/streamdown/dist/*.js"');
    });

    it("renders radicals, fractions, and superscripts without dropping the radicand", async () => {
      const { container } = renderMarkdownText(
        String.raw`$$x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}$$`,
      );

      await waitFor(() => expect(container.querySelector(".katex")).not.toBeNull());
      const katex = container.querySelector(".katex") as HTMLElement;

      expect(katex.querySelector(".mfrac")).not.toBeNull();
      expect(katex.querySelector(".sqrt")).not.toBeNull();
      expect(katex.querySelector(".vlist")).not.toBeNull();
      expect(katex.textContent).toContain("b");
      expect(katex.textContent).toContain("2");
      expect(katex.textContent).toContain("4");
      expect(katex.textContent).toContain("a");
      expect(katex.textContent).toContain("c");
    });

    it("renders multi-term square roots used by distance formulas", async () => {
      const { container } = renderMarkdownText(
        String.raw`$$d = \sqrt{(x_2 - x_1)^2 + (y_2 - y_1)^2}$$`,
      );

      await waitFor(() => expect(container.querySelector(".katex")).not.toBeNull());
      const katex = container.querySelector(".katex") as HTMLElement;

      expect(katex.querySelector(".sqrt")).not.toBeNull();
      expect(katex.textContent).toContain("x");
      expect(katex.textContent).toContain("y");
      expect(katex.textContent).toContain("1");
      expect(katex.textContent).toContain("2");
    });

    it("keeps invalid math visible instead of swallowing the message", async () => {
      const { container } = renderMarkdownText(String.raw`$$\sqrt{$$`);

      await waitFor(() => {
        expect(container.textContent).toContain("\\sqrt");
      });
      expect(container.textContent).not.toBe("");
    });

    it("recovers from an incomplete streamed radical to the final KaTeX output", async () => {
      const streamingItem = (text: string): RenderItem[] => [
        { kind: "text", itemId: null, text, final: false },
      ];
      const renderStreamingMath = (text: string) => (
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={streamingItem(text)} sessionStatus="running" />
        </FileViewerContext.Provider>
      );

      const { container, rerender } = render(renderStreamingMath(String.raw`$$\sqrt{`));
      await waitFor(() => expect(container.textContent).toContain("\\sqrt"));

      rerender(renderStreamingMath(String.raw`$$\sqrt{x + 1}$$`));

      await waitFor(() => expect(container.querySelector(".katex")).not.toBeNull());
      const katex = container.querySelector(".katex") as HTMLElement;
      expect(katex.querySelector(".sqrt")).not.toBeNull();
      expect(katex.textContent).toContain("x");
      expect(katex.textContent).toContain("1");
    });
  });

  // Proves the markdown throttle is actually wired into the render path (not
  // just unit-tested in isolation): a regression that drops `useThrottledValue`
  // from `FilePathAwareMessageResponse` would let the live bubble re-parse on
  // every commit, turning the "not yet CHARLIE" assertion red.
  describe("streaming markdown throttle", () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    // A streaming text item (itemId null → stable `text:<index>` key, so the
    // same throttle instance persists across re-renders as the text grows).
    const streamingText = (text: string): RenderItem[] => [
      { kind: "text", itemId: null, text, final: false },
    ];
    const renderStreaming = (text: string) => (
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <BlockRenderer items={streamingText(text)} sessionStatus="running" />
      </FileViewerContext.Provider>
    );

    it("defers re-parse of a within-window change, then converges to the latest", () => {
      const { rerender, container } = render(renderStreaming("ALPHA"));
      expect(container.textContent).toContain("ALPHA");

      // First change after mount emits immediately (snappy first-token paint).
      act(() => {
        rerender(renderStreaming("ALPHA BRAVO"));
      });
      expect(container.textContent).toContain("BRAVO");

      // A further change within the throttle window must NOT re-parse yet —
      // this is the assertion that fails if the throttle is removed (the bubble
      // would re-parse on the commit and show CHARLIE immediately).
      act(() => {
        rerender(renderStreaming("ALPHA BRAVO CHARLIE"));
        vi.advanceTimersByTime(20);
      });
      expect(container.textContent).toContain("BRAVO");
      expect(container.textContent).not.toContain("CHARLIE");

      // Past the window → the trailing flush re-parses with the latest text.
      act(() => {
        vi.advanceTimersByTime(100);
      });
      expect(container.textContent).toContain("CHARLIE");
    });
  });

  // A text block carrying a ~50KB unbroken base64 data URL (an
  // image block accidentally serialized into the text stream) froze the tab —
  // the full markdown pipeline parsed it and the browser tried to lay out one
  // unbreakable ~50K-char line. The renderer now routes a pathological block to
  // a plain, break-anywhere fallback that bypasses markdown.
  describe("pathological text guard", () => {
    const renderText = (text: string) =>
      render(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer
            items={[{ kind: "text", itemId: "t1", text, final: true }]}
            sessionStatus="idle"
          />
        </FileViewerContext.Provider>,
      );

    it("renders a giant unbroken token via the break-anywhere fallback", () => {
      // A long base64-ish token with no whitespace — exactly the freezing
      // blob shape. It must land in the plain fallback (a `break-all` element), not
      // the markdown pipeline, so the layout engine has break opportunities.
      const blob = `data:image/png;base64,${"A".repeat(10_000)}`;
      const { container } = renderText(blob);

      const el = container.querySelector(".break-all");
      expect(el).not.toBeNull();
      // The text is shown in full (under the 200K display cap) — no elision.
      expect(el!.textContent).toContain(blob);
      expect(el!.textContent).not.toContain("more characters not shown");
    });

    it("elides a payload past the plaintext display cap", () => {
      // 250K is chosen to sit above the 200K MAX_PLAINTEXT_DISPLAY_LENGTH cap so
      // it exercises the elision path: the DOM node must not grow without bound,
      // so the tail past 200K is dropped and an elision marker appended.
      const blob = "x".repeat(250_000);
      const { container } = renderText(blob);

      const el = container.querySelector(".break-all");
      expect(el).not.toBeNull();
      expect(el!.textContent).toContain("more characters not shown");
      // Painted text is 200K shown + a short marker — strictly less than the
      // full 250K input. A regression that dropped the cap would render all 250K.
      expect(el!.textContent!.length).toBeLessThan(250_000);
    });

    it("leaves normal prose on the markdown path (no fallback)", async () => {
      // A short, whitespace-broken string is NOT pathological — it must still
      // flow through markdown. The `**bold**` proves it: Streamdown renders the
      // emphasis as a `data-streamdown="strong"` span, which the plain break-all
      // fallback never would. So this fails both if the fallback wrongly fires
      // AND if the guard somehow routed normal prose to plaintext.
      const { container } = renderText("This is a **perfectly** normal message.");

      expect(container.querySelector(".break-all")).toBeNull();
      // Wait for Streamdown to parse the markdown, then confirm the emphasis
      // became its strong span (proving the markdown path actually ran).
      const strong = await screen.findByText("perfectly", {
        selector: '[data-streamdown="strong"]',
      });
      expect(strong).not.toBeNull();
    });
  });

  it("renders fenced code blocks inside a <pre> wrapper", async () => {
    // Regression: the file-path-aware override used to live in Streamdown's
    // `code` slot, which fires for both inline AND fenced blocks. The block
    // fallback returned a bare <code>, stripping the <pre> wrapper and
    // collapsing whitespace. The override now lives in the `inlineCode`
    // slot so fenced blocks keep Streamdown's default rendering.
    const items: RenderItem[] = [
      {
        kind: "text",
        itemId: "t1",
        text: "Here is some code:\n\n```python\ndef foo():\n    return 1\n```\n",
        final: true,
      },
    ];
    render(
      <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
        <BlockRenderer items={items} sessionStatus="idle" />
      </FileViewerContext.Provider>,
    );
    // Wait for Streamdown to finish parsing the (streamed) markdown.
    const pre = await screen.findByText(/def foo/, { selector: "pre, pre *" });
    expect(pre.closest("pre")).not.toBeNull();
  });
});

// ── Inline file-path linkification ───────────────────────────────────────────
//
// Inline-code spans that name a real workspace file become clickable links
// that open the FileViewer. Coverage was previously gated on the agent
// *changed-files* list, so a file the agent only *referenced* (present on
// disk but not modified this session) rendered as inert code. These tests
// pin the broader rule: any path-shaped span pointing at a file that exists
// in the workspace is linkified, verified against the runner filesystem API.

const EXISTING_PATH = "projects/dais-2026-outlines/foo.md";
const EXISTING_PARENT = "projects/dais-2026-outlines";

function dirListingResponse(parent: string, names: string[]): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      object: "list",
      data: names.map((name) => ({
        id: `${parent}/${name}`,
        name,
        path: `${parent}/${name}`,
        type: "file",
        bytes: 10,
        modified_at: 1,
      })),
      has_more: false,
    }),
  } as unknown as Response;
}

/** Root-directory listing: entries carry bare-basename paths (no parent). */
function rootListingResponse(names: string[]): Response {
  return {
    ok: true,
    status: 200,
    json: async () => ({
      object: "list",
      data: names.map((name) => ({
        id: name,
        name,
        path: name,
        type: "file",
        bytes: 10,
        modified_at: 1,
      })),
      has_more: false,
    }),
  } as unknown as Response;
}

const NOT_FOUND_RESPONSE = {
  ok: false,
  status: 404,
  statusText: "Not Found",
  json: async () => ({ error: { code: "not_found" } }),
} as unknown as Response;

interface TestFileViewerContext {
  openFile: (path: string) => void;
  isChangedPath: (path: string) => boolean;
  conversationId: string | undefined;
  workspaceRoot: string | null;
  workspaceHome: string | null;
}

function TestProviders({
  children,
  queryClient,
  fileViewerContext,
}: {
  children: ReactNode;
  queryClient: QueryClient;
  fileViewerContext: TestFileViewerContext;
}) {
  return (
    <QueryClientProvider client={queryClient}>
      <FileViewerContext.Provider value={fileViewerContext}>{children}</FileViewerContext.Provider>
    </QueryClientProvider>
  );
}

function renderMessage(
  text: string,
  ctx: {
    openFile: (path: string) => void;
    isChangedPath: (path: string) => boolean;
    conversationId: string | undefined;
    workspaceRoot?: string | null;
    workspaceHome?: string | null;
  },
) {
  const fullCtx: TestFileViewerContext = {
    workspaceRoot: null,
    workspaceHome: null,
    ...ctx,
  };
  const items: RenderItem[] = [{ kind: "text", itemId: "t1", text, final: true }];
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return render(
    <TestProviders queryClient={qc} fileViewerContext={fullCtx}>
      <BlockRenderer items={items} sessionStatus="idle" />
    </TestProviders>,
  );
}

describe("BlockRenderer inline file-path linkification", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetAllMocks();
  });

  it("linkifies a file that exists in the workspace but was not agent-changed", async () => {
    // Repro for the reported bug: the file is real (present on disk) but not
    // in the changed-files list, so the old changed-files-only gate left it as
    // plain code. It must now resolve via the filesystem existence check.
    fetchMock.mockResolvedValue(dirListingResponse(EXISTING_PARENT, ["foo.md"]));
    const openFile = vi.fn();
    renderMessage(`I added this to \`${EXISTING_PATH}\` already.`, {
      openFile,
      isChangedPath: () => false,
      conversationId: "conv_1",
    });

    // The span becomes a clickable button once the parent-dir listing
    // confirms the file exists. A failure here means the existence check
    // didn't run or didn't linkify a real, unchanged workspace file.
    const link = await screen.findByRole("button", { name: EXISTING_PATH });
    link.click();
    expect(openFile).toHaveBeenCalledWith(EXISTING_PATH);

    // Existence is checked by listing the PARENT directory, not by reading
    // the file content or walking the whole tree.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain(`/filesystem/${EXISTING_PARENT}`);
  });

  it("leaves a path-shaped span as plain code when no such file exists", async () => {
    // Parent dir listing comes back 404 (or without the file) → not a real
    // file → must stay inert code, never a link. Guards against linkifying
    // every path-shaped string.
    fetchMock.mockResolvedValue(NOT_FOUND_RESPONSE);
    const openFile = vi.fn();
    renderMessage("See `projects/ghost/missing.md` for details.", {
      openFile,
      isChangedPath: () => false,
      conversationId: "conv_1",
    });

    // The existence check must have fired (path-shaped) and resolved negative.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const span = await screen.findByText("projects/ghost/missing.md");
    expect(span.tagName).toBe("CODE");
    expect(screen.queryByRole("button", { name: "projects/ghost/missing.md" })).toBeNull();
  });

  it("links an agent-changed file without any filesystem round-trip", async () => {
    // Changed files are known synchronously (and may be uncommitted or
    // deleted), so they must linkify with zero network calls — the fast path.
    const openFile = vi.fn();
    renderMessage("Edited `src/app/main.ts` just now.", {
      openFile,
      isChangedPath: (p) => p === "src/app/main.ts",
      conversationId: "conv_1",
    });

    const link = await screen.findByRole("button", { name: "src/app/main.ts" });
    link.click();
    expect(openFile).toHaveBeenCalledWith("src/app/main.ts");
    // No existence check needed when the path is already a known change.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not treat non-path inline code as a file (no spurious fetch)", async () => {
    // `git status` has whitespace and no directory segment → fails the
    // path-shape heuristic, so no existence request is made and it stays code.
    const openFile = vi.fn();
    renderMessage("Run `git status` to check.", {
      openFile,
      isChangedPath: () => false,
      conversationId: "conv_1",
    });

    const span = await screen.findByText("git status");
    expect(span.tagName).toBe("CODE");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("linkifies a '~'-relative path under the workspace root, opening the relative path", async () => {
    // The reported bug: the agent writes `~/ws/foo.md` while the working dir
    // is `~/ws`. With home + root known, this resolves to the root-level file
    // `foo.md` — a bare basename the path-shape heuristic alone would reject.
    // Existence is checked by listing the workspace ROOT (bare /filesystem).
    // Root-level entries carry bare-basename paths (no parent prefix).
    fetchMock.mockResolvedValue(rootListingResponse(["foo.md"]));
    const openFile = vi.fn();
    renderMessage("I wrote `~/ws/foo.md` for you.", {
      openFile,
      isChangedPath: () => false,
      conversationId: "conv_1",
      workspaceRoot: "/home/u/ws",
      workspaceHome: "/home/u",
    });

    // The span shows the original `~/ws/foo.md` text but links to the resolved
    // workspace-relative `foo.md` — failure means tilde-expand/strip-root or
    // the root-level existence check broke.
    const link = await screen.findByRole("button", { name: "~/ws/foo.md" });
    link.click();
    expect(openFile).toHaveBeenCalledWith("foo.md");
    // Parent of a root-level file is the workspace root → bare /filesystem,
    // not /filesystem/<dir>.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/environments/default/filesystem?");
  });

  it("linkifies an absolute path under the workspace root", async () => {
    // Absolute paths were rejected outright before; now an absolute path under
    // the root strips to its relative form and links.
    fetchMock.mockResolvedValue(dirListingResponse("src", ["app.ts"]));
    const openFile = vi.fn();
    renderMessage("See `/home/u/ws/src/app.ts`.", {
      openFile,
      isChangedPath: () => false,
      conversationId: "conv_1",
      workspaceRoot: "/home/u/ws",
      workspaceHome: "/home/u",
    });

    const link = await screen.findByRole("button", { name: "/home/u/ws/src/app.ts" });
    link.click();
    expect(openFile).toHaveBeenCalledWith("src/app.ts");
    expect(fetchMock.mock.calls[0][0]).toContain("/filesystem/src?");
  });

  it("leaves an absolute path OUTSIDE the workspace root as plain code (no fetch)", async () => {
    // `/etc/hosts` is absolute but not under the root → unresolvable → must
    // never linkify, and must not trigger an existence listing.
    const openFile = vi.fn();
    renderMessage("Check `/etc/hosts` on the box.", {
      openFile,
      isChangedPath: () => false,
      conversationId: "conv_1",
      workspaceRoot: "/home/u/ws",
      workspaceHome: "/home/u",
    });

    const span = await screen.findByText("/etc/hosts");
    expect(span.tagName).toBe("CODE");
    expect(screen.queryByRole("button", { name: "/etc/hosts" })).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

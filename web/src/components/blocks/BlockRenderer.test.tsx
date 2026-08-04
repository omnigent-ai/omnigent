// BlockRenderer dispatch wiring. The kind→component switch is easy
// to break by removing a case — neither the walker nor the
// individual card tests catch that. Drive it directly here.

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RenderItem } from "@/lib/renderItems";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { BlockRenderer } from "./BlockRenderer";

afterEach(cleanup);

const FILE_VIEWER_NOOP = {
  openFile: () => {},
  isChangedPath: () => false,
  conversationId: undefined,
  workspaceRoot: null,
  workspaceHome: null,
};

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
      // (a later turn is the live one, not this bubble).
      rerender(
        <FileViewerContext.Provider value={FILE_VIEWER_NOOP}>
          <BlockRenderer items={items} sessionStatus="running" turnLifecycle="completed" />
        </FileViewerContext.Provider>,
      );
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
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
      // Fold is present AND its trace is still on screen (mounted open).
      expect(screen.getByTestId("turn-worked-fold")).toBeDefined();
      expect(screen.getByText("Looking around.")).toBeDefined();

      // It closes itself on the next frame.
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

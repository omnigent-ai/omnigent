import { createElement } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type { RenderItem } from "@/lib/renderItems";
import { ToolCard, ToolGroupSummary, formatToolDuration, getOutputPreview } from "./ToolCard";

afterEach(cleanup);

// Render helper: ToolCard reads a Tooltip (trigger title) so it needs the
// TooltipProvider; createElement keeps this in a .ts file without JSX.
function renderCard(props: Parameters<typeof ToolCard>[0]) {
  return render(createElement(TooltipProvider, null, createElement(ToolCard, props)));
}

function renderFileToolCard(
  viewer: {
    openFile: (path: string) => void;
    workspaceRoot: string | null;
    workspaceHome: string | null;
  },
  card: { name: string; arguments: Record<string, unknown> },
) {
  const ctx = {
    ...viewer,
    openGithubTab: () => {},
    isChangedPath: () => false,
    conversationId: "c1",
  };
  return render(
    createElement(
      TooltipProvider,
      null,
      createElement(
        FileViewerContext.Provider,
        { value: ctx },
        createElement(ToolCard, { ...card, output: null, state: "output-available" }),
      ),
    ),
  );
}

describe("formatToolDuration", () => {
  it("formats subsecond, second, minute, and hour durations", () => {
    expect(formatToolDuration(0.042)).toBe("42ms");
    expect(formatToolDuration(3.25)).toBe("3.3s");
    expect(formatToolDuration(12.4)).toBe("12s");
    expect(formatToolDuration(61.2)).toBe("1m 1s");
    expect(formatToolDuration(3_599.9)).toBe("1h 0m");
    expect(formatToolDuration(3_901)).toBe("1h 5m");
  });

  it("handles invalid or negative durations as zero milliseconds", () => {
    expect(formatToolDuration(Number.NaN)).toBe("0ms");
    expect(formatToolDuration(-1)).toBe("0ms");
  });
});

describe("getOutputPreview", () => {
  it("keeps short output intact", () => {
    const preview = getOutputPreview("first\nsecond");

    expect(preview.text).toBe("first\nsecond");
    expect(preview.isTruncated).toBe(false);
    expect(preview.lineCount).toBe(2);
    expect(preview.hiddenLineCount).toBe(0);
    expect(preview.hiddenCharCount).toBe(0);
  });

  it("truncates long output by line count and reports hidden content", () => {
    const output = Array.from({ length: 85 }, (_, index) => `line ${index + 1}`).join("\n");
    const preview = getOutputPreview(output);

    expect(preview.isTruncated).toBe(true);
    expect(preview.shownLineCount).toBe(80);
    expect(preview.hiddenLineCount).toBe(5);
    expect(preview.text).toContain("line 80");
    expect(preview.text).not.toContain("line 81");
  });

  it("expands long output back to the full text", () => {
    const output = "x".repeat(12_050);
    const collapsed = getOutputPreview(output);
    const expanded = getOutputPreview(output, true);

    expect(collapsed.isTruncated).toBe(true);
    expect(collapsed.shownCharCount).toBe(12_000);
    expect(expanded.text).toBe(output);
    expect(expanded.isTruncated).toBe(false);
  });
});

describe("ToolCard rendering", () => {
  it("renders the tool title and duration in the collapsed trigger row", () => {
    // WHY: the trigger row is the always-visible summary; an unknown tool name
    // falls back to `name(argsSummary)`, and a completed duration renders.
    renderCard({
      name: "my_tool",
      argsSummary: "x=1",
      arguments: { x: 1 },
      output: "done",
      state: "output-available",
      duration: 3.25,
    });
    expect(screen.getByText("my_tool(x=1)")).toBeInTheDocument();
    expect(screen.getByText("3.3s")).toBeInTheDocument();
  });

  it("expands to reveal the Parameters panel and output on click", () => {
    // WHY: clicking the trigger must reveal the parameters JSON and the output
    // section — the collapsed-by-default content path.
    const { container } = renderCard({
      name: "my_tool",
      argsSummary: "",
      arguments: { a: 2 },
      output: "the output text",
      state: "output-available",
    });
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    fireEvent.click(trigger);
    expect(screen.getAllByText("Parameters").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Output").length).toBeGreaterThan(0);
  });

  it("renders a pending output placeholder while input-available with no output", () => {
    // WHY: a running tool (input-available, output null) shows the
    // waiting-for-output indicator, not an empty/error panel.
    const { container } = renderCard({
      name: "my_tool",
      arguments: {},
      output: null,
      state: "input-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText(/Waiting for output/)).toBeInTheDocument();
  });

  it.each([
    ["cancelled", "Tool was cancelled before output arrived."],
    ["no-output", "No output was recorded for this tool call."],
    ["output-error", "Tool did not return output before the response failed."],
  ] as const)("renders the %s empty-output message", (state, message) => {
    // WHY: each terminal-without-output state maps to a distinct explanatory
    // message so the user understands why there's nothing to show.
    const { container } = renderCard({
      name: "my_tool",
      arguments: {},
      output: null,
      state,
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText(message)).toBeInTheDocument();
  });

  it("makes a workspace file path clickable for file-path tools inside a FileViewer", () => {
    // WHY: sys_os_read with a relative path renders the path as a role="link"
    // that calls the FileViewer's openFile; clicking it must not toggle the
    // collapsible (stopPropagation).
    const openFile = vi.fn();
    renderFileToolCard(
      { openFile, workspaceRoot: null, workspaceHome: null },
      { name: "sys_os_read", arguments: { path: "src/a.ts" } },
    );
    const link = screen.getByRole("link", { name: "src/a.ts" });
    fireEvent.click(link);
    expect(openFile).toHaveBeenCalledWith("src/a.ts");
  });

  it.each([
    ["/home/u/ws/src/new.ts", "src/new.ts"],
    ["/etc/hosts", "/etc/hosts"],
  ])("opens a Claude Write of %s in the FileViewer as %s", (filePath, opened) => {
    // WHY: Claude Code's file tools pass an absolute `file_path`; a file the
    // agent creates must open like a chat file link — workspace-relative when
    // under the root, host-absolute otherwise.
    const openFile = vi.fn();
    renderFileToolCard(
      { openFile, workspaceRoot: "/home/u/ws", workspaceHome: "/home/u" },
      { name: "Write", arguments: { file_path: filePath, content: "x" } },
    );
    fireEvent.click(screen.getByRole("link", { name: filePath }));
    expect(openFile).toHaveBeenCalledWith(opened);
  });

  it("keeps an absolute path plain text until the workspace root is known", () => {
    // WHY: with no root, inside vs outside the workspace is undecidable.
    renderFileToolCard(
      { openFile: vi.fn(), workspaceRoot: null, workspaceHome: null },
      { name: "sys_os_read", arguments: { path: "/etc/hosts" } },
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("/etc/hosts")).toBeInTheDocument();
  });

  it("soft-wraps panel content by default so long lines never clip", () => {
    // WHY: a long single-line tool output used to keep `white-space: pre`
    // and could only be read by scrolling the panel horizontally.
    const { container } = renderCard({
      name: "my_tool",
      arguments: {},
      output: "y".repeat(2000),
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);

    // Both the Parameters and Output panels default to wrapping.
    const pres = Array.from(container.querySelectorAll("pre"));
    expect(pres.length).toBeGreaterThan(1);
    for (const pre of pres) {
      expect(pre.className).toContain("whitespace-pre-wrap");
    }
  });

  it("word-wrap toggle restores the horizontal-scroll view per panel", () => {
    // WHY: column-aligned output (tables, diffs) sometimes reads better
    // unwrapped, so each panel's toggle must flip it back to `pre`.
    const { container } = renderCard({
      name: "my_tool",
      arguments: {},
      output: "the output",
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);

    const toggles = screen.getAllByRole("button", { name: "Disable word wrap" });
    expect(toggles.length).toBe(2); // Parameters + Output
    fireEvent.click(toggles[0]);

    // Only the toggled panel unwraps; the other keeps the default.
    const wrapped = Array.from(container.querySelectorAll("pre")).filter((pre) =>
      pre.className.includes("whitespace-pre-wrap"),
    );
    expect(wrapped.length).toBe(1);
    expect(screen.getByRole("button", { name: "Enable word wrap" })).toBeInTheDocument();
  });
});

describe("ToolCard shell terminal panel", () => {
  function expand(container: HTMLElement) {
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
  }

  it("shows the unwrapped command terminal-style in the row and the panel", () => {
    // WHY: shell calls should read like a terminal, not a JSON payload with
    // the login-shell wrapper and escaped quotes.
    const { container } = renderCard({
      name: "shell",
      arguments: { command: "/bin/zsh -lc 'pnpm install'", cwd: "/repo/app" },
      output: "Done in 1.2s",
      state: "output-available",
    });
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    expect(trigger).toHaveAttribute("title", "pnpm install");
    expect(trigger.textContent).toContain("$ pnpm install");
    // The prompt is decorative, so the row's accessible name is the bare command.
    expect(screen.getByRole("button", { name: /^pnpm install/ })).toBe(trigger);

    expand(container);
    const terminal = screen.getByTestId("shell-tool-terminal");
    expect(terminal.textContent).toContain("/repo/app");
    // The `$` prompt is CSS-only, so the text is the bare command.
    expect(terminal.textContent).toContain("pnpm install");
    expect(terminal.textContent).not.toContain("zsh -lc");
    expect(terminal.textContent).toContain("Done in 1.2s");
    expect(screen.queryByText("Parameters")).toBeNull();
  });

  it("reveals the raw parameters and untouched output on demand", () => {
    // WHY: the original arguments and output stay available for debugging,
    // with control characters made visible instead of hidden or executed.
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "ls" },
      output: "\u001b[1ma.txt\u001b[0m",
      state: "output-available",
    });
    expand(container);
    fireEvent.click(screen.getByRole("button", { name: "Raw" }));
    expect(screen.getByText("Parameters")).toBeInTheDocument();
    expect(screen.getByText("Output")).toBeInTheDocument();
    expect(container.textContent).toContain("\\u001b[1ma.txt\\u001b[0m");
  });

  it("reads the exit code in the output format of the tool that ran", () => {
    // WHY: the panel must hand the parser the tool's own format; the suffix
    // must not leak into the output.
    const { container } = renderCard({
      name: "shell",
      arguments: { command: "make" },
      output: "missing\n[exit code: 1]",
      state: "output-available",
    });
    expand(container);
    const terminal = screen.getByTestId("shell-tool-terminal");
    expect(screen.getByText("exit 1")).toBeInTheDocument();
    expect(terminal.textContent).not.toContain("[exit code");
  });

  it("copies the command as run and the output without escapes", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const { container } = renderCard({
      name: "shell",
      arguments: { command: "/bin/zsh -lc 'git diff --stat'" },
      output: "\u001b[32m+\u001b[m 1 file",
      state: "output-available",
    });
    expand(container);
    fireEvent.click(screen.getByRole("button", { name: "Copy command" }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("git diff --stat"));
    fireEvent.click(screen.getByRole("button", { name: "Copy output" }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("+ 1 file"));
  });

  it("shows a Bash description in the row and the command in the panel", () => {
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "ls docs", description: "List docs directory" },
      output: "README.md",
      state: "output-available",
    });
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    expect(trigger.textContent).toContain("List docs directory");
    expand(container);
    expect(screen.getByTestId("shell-tool-terminal").textContent).toContain("ls docs");
  });

  it("keeps focus on the wrap toggle when there is no working directory", () => {
    // WHY: the actions sit beside the command without a cwd line; toggling wrap
    // must restyle them in place, not remount them and drop keyboard focus.
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "make test" },
      output: "ok",
      state: "output-available",
    });
    expand(container);
    const toggle = screen.getByRole("button", { name: "Disable word wrap" });
    toggle.focus();
    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "Enable word wrap" })).toBe(toggle);
    expect(document.activeElement).toBe(toggle);
  });

  it("shows a running status while the command has no output yet", () => {
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "sleep 5" },
      output: null,
      state: "input-available",
    });
    expand(container);
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it.each([
    ["output-error", "failed", "Tool did not return output before the response failed."],
    ["cancelled", "cancelled", "Tool was cancelled before output arrived."],
    ["no-output", "no output recorded", "No output was recorded for this tool call."],
  ] as const)("reports %s once, inside the terminal", (state, status, genericMessage) => {
    // WHY: the terminal's status line already says it; the generic box repeated it.
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "make" },
      output: null,
      state,
    });
    expand(container);
    expect(screen.getByText(status)).toBeInTheDocument();
    expect(screen.queryByText(genericMessage)).toBeNull();
  });

  it("shows the working directory sys_os_shell reports in its result", () => {
    // WHY: sys_os_shell has no cwd argument; its result carries the real one.
    const { container } = renderCard({
      name: "sys_os_shell",
      arguments: { command: "pwd" },
      output: JSON.stringify({ stdout: "/repo/web\n", stderr: "", exit_code: 0, cwd: "/repo/web" }),
      state: "output-available",
    });
    expand(container);
    expect(screen.getByTitle("/repo/web")).toBeInTheDocument();
  });

  it("shows when sys_os_shell killed a command for timing out", () => {
    const { container } = renderCard({
      name: "sys_os_shell",
      arguments: { command: "sleep 99" },
      output: JSON.stringify({ stdout: "", stderr: "", exit_code: null, timed_out: true }),
      state: "output-available",
    });
    expand(container);
    expect(screen.getByText("timed out")).toBeInTheDocument();
  });

  it("dims a leading env assignment in the collapsed row", () => {
    // WHY: long PATH=... prefixes otherwise bury the program being run.
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: 'PATH="/opt/bin:$PATH" pnpm test' },
      output: "",
      state: "output-available",
    });
    const dimmed = container.querySelector(".opacity-60");
    expect(dimmed?.textContent).toBe('PATH="/opt/bin:$PATH" ');
  });
});

describe("ToolGroupSummary", () => {
  function toolItem(callId: string, name: string): RenderItem {
    return {
      kind: "tool",
      execution: { callId, name, argsSummary: "", arguments: {} },
      output: "ok",
      state: "output-available",
      startedAt: null,
      duration: 1,
    } as unknown as RenderItem;
  }

  it("labels unrecognized hidden tools generically and renders children when expanded", () => {
    // WHY: several unrecognized tools get the generic "Called N tools",
    // and expanding mounts each tool card.
    const { container } = render(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, {
          tools: [toolItem("t1", "alpha_tool"), toolItem("t2", "beta_tool")],
        }),
      ),
    );
    expect(screen.getByText("Called 2 tools")).toBeInTheDocument();
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("alpha_tool")).toBeInTheDocument();
    expect(screen.getByText("beta_tool")).toBeInTheDocument();
  });

  it("uses the singular 'tool' for one hidden unrecognized tool", () => {
    render(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, { tools: [toolItem("t1", "solo_tool")] }),
      ),
    );
    expect(screen.getByText("Called 1 tool")).toBeInTheDocument();
  });

  it("labels recognized hidden tools with the semantic CLI-style phrase", () => {
    // WHY: recognized tool names (Claude Code's Bash/Read) replace the
    // bare count with the action summary the native CLI prints.
    const { rerender } = render(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, {
          tools: [toolItem("t1", "Read"), toolItem("t2", "Read")],
        }),
      ),
    );
    expect(screen.getByText("Read 2 files")).toBeInTheDocument();

    rerender(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, {
          tools: [toolItem("t1", "Bash"), toolItem("t2", "Read"), toolItem("t3", "Read")],
        }),
      ),
    );
    expect(screen.getByText("Ran 1 shell command, read 2 files")).toBeInTheDocument();
  });
});

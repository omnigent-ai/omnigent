import { createElement } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    const ctx = {
      openFile,
      isChangedPath: () => false,
      conversationId: "c1",
      workspaceRoot: null,
      workspaceHome: null,
    };
    render(
      createElement(
        TooltipProvider,
        null,
        createElement(
          FileViewerContext.Provider,
          { value: ctx },
          createElement(ToolCard, {
            name: "sys_os_read",
            arguments: { path: "src/a.ts" },
            output: null,
            state: "output-available",
          }),
        ),
      ),
    );
    const link = screen.getByRole("link", { name: "src/a.ts" });
    fireEvent.click(link);
    expect(openFile).toHaveBeenCalledWith("src/a.ts");
  });

  it("does not linkify an absolute file path (FileViewer rejects absolute paths)", () => {
    // WHY: the FileViewer can't resolve absolute paths, so an absolute
    // sys_os_read path must render as plain text, never a clickable link.
    const ctx = {
      openFile: vi.fn(),
      isChangedPath: () => false,
      conversationId: "c1",
      workspaceRoot: null,
      workspaceHome: null,
    };
    render(
      createElement(
        TooltipProvider,
        null,
        createElement(
          FileViewerContext.Provider,
          { value: ctx },
          createElement(ToolCard, {
            name: "sys_os_read",
            arguments: { path: "/etc/hosts" },
            output: null,
            state: "output-available",
          }),
        ),
      ),
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText("/etc/hosts")).toBeInTheDocument();
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

  it("labels the run with a pluralized step count and renders children when expanded", () => {
    // WHY: the summary line counts the full contiguous run; ">1" pluralizes
    // "steps", and expanding mounts each tool card.
    const { container } = render(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, {
          tools: [toolItem("t1", "alpha_tool"), toolItem("t2", "beta_tool")],
        }),
      ),
    );
    expect(screen.getByText("See 2 steps")).toBeInTheDocument();
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("alpha_tool")).toBeInTheDocument();
    expect(screen.getByText("beta_tool")).toBeInTheDocument();
  });

  it("uses the singular 'step' for one tool and honors an explicit count override", () => {
    // WHY: n===1 drops the plural; `count` overrides tools.length so a
    // streaming tail isn't undercounted.
    const { rerender } = render(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, { tools: [toolItem("t1", "solo_tool")] }),
      ),
    );
    expect(screen.getByText("See 1 step")).toBeInTheDocument();

    rerender(
      createElement(
        TooltipProvider,
        null,
        createElement(ToolGroupSummary, { tools: [toolItem("t1", "solo_tool")], count: 5 }),
      ),
    );
    expect(screen.getByText("See 5 steps")).toBeInTheDocument();
  });
});

import { createElement } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type { RenderItem } from "@/lib/renderItems";
import { writeTerminalThemeMode } from "@/lib/terminalThemePreferences";
import { ToolCard, ToolGroupSummary, formatToolDuration } from "./ToolCard";
import { getOutputPreview } from "./ToolCodePanel";
import type * as ToolViewsModule from "./toolViews";
import type { ToolView } from "./toolViews";

// Tests register the views they exercise here, in place of the real registry.
const registeredViews = vi.hoisted(() => new Map<string, ToolView>());
vi.mock("./toolViews", () => ({ toolViewFor: (name: string) => registeredViews.get(name) }));

afterEach(() => {
  cleanup();
  registeredViews.clear();
});

// Render helper: ToolCard reads a Tooltip (trigger title) so it needs the
// TooltipProvider; createElement keeps this in a .ts file without JSX.
function renderCard(props: Parameters<typeof ToolCard>[0]) {
  return render(createElement(TooltipProvider, null, createElement(ToolCard, props)));
}

/** Registers the tool's real view in the test registry. */
async function registerRealView(name: string) {
  const actualViews = await vi.importActual<typeof ToolViewsModule>("./toolViews");
  registeredViews.set(name, actualViews.toolViewFor(name)!);
}

// Radix tabs switch on mouse down (or focus), not on a synthetic click.
function showView(name: "Fields" | "Diff" | "Raw") {
  fireEvent.mouseDown(screen.getByRole("tab", { name }));
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
  it("renders an unknown tool's humanized title and duration, with no line under a success", () => {
    // WHY: the always-visible trigger shows a humanized title and key argument values.
    const { container } = renderCard({
      name: "manage_todo_list",
      arguments: { operation: "write", todoList: [{ id: 1, title: "Plan", status: "completed" }] },
      output: "Task plan updated (1 task).",
      state: "output-available",
      duration: 3.25,
    });
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    expect(screen.getByText("Manage todo list")).toBeInTheDocument();
    expect(trigger.textContent).toContain("write · 1 item");
    expect(trigger).toHaveAttribute("title", "Manage todo list write · 1 item (manage_todo_list)");
    // WHY: a successful call's details wait behind the expand, so the row reads as its title.
    expect(trigger.textContent).not.toContain("Task plan updated");
    expect(screen.getByText("3.3s")).toBeInTheDocument();
    // WHY: row text follows the Interface font size.
    expect(trigger).toHaveClass("text-ui");
    expect(screen.getByText("Manage todo list")).toHaveClass("font-medium");
  });

  it("tags an MCP tool with its server and shows its failure only when expanded", () => {
    const { container } = renderCard({
      name: "mcp__github__create_pull_request",
      arguments: { owner: "omnigent-ai", repo: "omnigent", draft: true },
      output: "Error: 422 Validation Failed: A pull request already exists",
      state: "output-available",
    });
    expect(screen.getByText("Create pull request")).toBeInTheDocument();
    // A known server shows its brand spelling rather than the config name.
    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.queryByText("github")).toBeNull();
    const error = "Error: 422 Validation Failed: A pull request already exists";
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    expect(screen.queryByText(error)).toBeNull();
    fireEvent.click(trigger);
    expect(screen.getByTestId("tool-error-box")).toHaveTextContent(error);
    fireEvent.click(trigger);
    expect(screen.queryByText(error)).toBeNull();
  });

  it("expands to labeled Input and Result fields, with the raw JSON behind a Fields | Raw switch", () => {
    // WHY: the expanded view shows labeled fields, with the payloads one switch away.
    const { container } = renderCard({
      name: "my_tool",
      arguments: { includeComments: false, todoList: [{ title: "Find workflow", status: "done" }] },
      output: "the output text",
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("Input")).toBeInTheDocument();
    expect(screen.getByText("Include comments")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();
    expect(screen.getByText("Find workflow")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByText("the output text")).toBeInTheDocument();
    expect(screen.queryByText("Parameters")).toBeNull();

    // Both views stay visible, so the current one reads at a glance.
    const fields = screen.getByRole("tab", { name: "Fields" });
    const raw = screen.getByRole("tab", { name: "Raw" });
    expect(fields).toHaveAttribute("aria-selected", "true");
    expect(raw).toHaveAttribute("aria-selected", "false");

    showView("Raw");
    expect(raw).toHaveAttribute("aria-selected", "true");
    expect(fields).toHaveAttribute("aria-selected", "false");
    // Raw panels keep the section names from the Fields view.
    expect(screen.getByText("Input")).toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy input" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy result" })).toBeInTheDocument();
    expect(screen.queryByText("Parameters")).toBeNull();
    expect(screen.queryByText("Output")).toBeNull();
    expect(container.textContent).toContain('"includeComments": false');
    // The switch stays put, so keyboard focus survives switching views.
    expect(screen.getByRole("tab", { name: "Raw" })).toBe(raw);

    showView("Fields");
    expect(screen.getByText("Input")).toBeInTheDocument();
    expect(screen.queryByText("Parameters")).toBeNull();
  });

  it("renders an MCP content-block JSON result as fields", () => {
    const issue = { id: "OMNI-12243", title: "Claude mod replaces tmux paste", state: "In Review" };
    const { container } = renderCard({
      name: "mcp__linear__get_issue",
      arguments: { id: "OMNI-12243" },
      output: JSON.stringify([{ type: "text", text: JSON.stringify(issue) }]),
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    const views = screen.getAllByTestId("tool-value-view");
    expect(views).toHaveLength(2);
    expect(views[1]!.textContent).toContain("TitleClaude mod replaces tmux paste");
    expect(views[1]!.textContent).toContain("StateIn Review");
  });

  it("keeps a JSON result too long to show in full in the expandable preview", () => {
    // WHY: long results (e.g. a sub-agent's session history) must keep the
    // truncated preview with its Expand control rather than render every field.
    const items = Array.from({ length: 60 }, (_, index) => ({ index, text: `item ${index}` }));
    const { container } = renderCard({
      name: "sys_session_get_history",
      arguments: {},
      output: JSON.stringify({ items }),
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.queryByTestId("tool-value-view")).toBeNull();
    expect(screen.getByRole("button", { name: "Expand" })).toBeInTheDocument();
  });

  it.each([{ mode: "visual" }, {}])(
    "names a text result once in Fields and titles its panel in Raw, given input %o",
    (args) => {
      // WHY: the Result label already names the panel, so a title would repeat it.
      const { container } = renderCard({
        name: "component_audit",
        arguments: args,
        output: "line 1\nline 2",
        state: "output-available",
      });
      fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
      expect(screen.getAllByText("Result")).toHaveLength(1);
      expect(screen.getByRole("button", { name: "Copy result" })).toBeInTheDocument();
      showView("Raw");
      expect(screen.getAllByText("Result")).toHaveLength(1);
    },
  );

  it.each([
    {
      producer: "partial failure",
      name: "search_issues",
      output: { error: "2 of 3 pages failed", results: ["OMNI-1"] },
      error: "2 of 3 pages failed",
      fields: "ResultsOMNI-1",
    },
    {
      producer: "failed SDK Pi result",
      name: "grep",
      output: {
        content: [{ type: "text", text: "Path not found: /repo/missing" }],
        details: { path: "/repo/missing" },
        isError: true,
      },
      error: "Path not found: /repo/missing",
      fields: "DetailsPath/repo/missing",
    },
    {
      producer: "failed SDK Pi result with no text",
      name: "read",
      output: { content: [{ type: "image", data: "aGk=", mimeType: "image/png" }], isError: true },
      error: "The tool reported an error.",
      fields: "Content1TypeimageDataaGk=Mime typeimage/png",
    },
    {
      producer: "failed result with its own error",
      name: "fetch",
      output: {
        error: "Timed out",
        content: [{ type: "text", text: "Partial page" }],
        isError: true,
      },
      error: "Timed out",
      fields: "Content1Partial pageType text",
    },
  ])("shows a $producer above the record's other fields", ({ name, output, error, fields }) => {
    // WHY: a failure can still return data; hiding it behind Raw loses the useful part.
    const { container } = renderCard({
      name,
      arguments: {},
      output: JSON.stringify(output),
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByTestId("tool-error-box")).toHaveTextContent(error);
    expect(screen.getByTestId("tool-value-view").textContent).toBe(fields);
  });

  it("keeps a short result with a trailing newline inline", () => {
    const { container } = renderCard({
      name: "my_tool",
      arguments: {},
      output: "ok\n",
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(container.querySelector("pre")).toBeNull();
    expect(screen.getAllByText("ok").length).toBeGreaterThan(0);
  });

  it("falls back to the output panel when a JSON result cannot be re-serialized", () => {
    // WHY: very deep JSON can parse yet exceed JSON.stringify's stack.
    const stringify = JSON.stringify;
    const spy = vi.spyOn(JSON, "stringify").mockImplementation((value, ...rest) => {
      if (value !== null && typeof value === "object" && "deep" in value) {
        throw new RangeError("Maximum call stack size exceeded");
      }
      return stringify(value, ...rest);
    });
    try {
      const { container } = renderCard({
        name: "my_tool",
        arguments: {},
        output: '{"deep": {"a": 1}}',
        state: "output-available",
      });
      fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
      expect(screen.queryByTestId("tool-value-view")).toBeNull();
      expect(screen.getByRole("button", { name: "Copy result" })).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it("shows a tool's reported failure in place of its result", () => {
    const { container } = renderCard({
      name: "sys_os_write",
      arguments: { path: "out.txt", content: "x" },
      output: '{"error": "permission denied"}',
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByTestId("tool-error-box")).toHaveTextContent("permission denied");
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
    // under the root, host-absolute otherwise. The row shows that path.
    const openFile = vi.fn();
    renderFileToolCard(
      { openFile, workspaceRoot: "/home/u/ws", workspaceHome: "/home/u" },
      { name: "Write", arguments: { file_path: filePath, content: "x" } },
    );
    fireEvent.click(screen.getByRole("link", { name: opened }));
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
    showView("Raw");

    // Both the Input and Result panels default to wrapping.
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
    showView("Raw");

    const toggles = screen.getAllByRole("button", { name: "Disable word wrap" });
    expect(toggles.length).toBe(2); // Input + Result
    fireEvent.click(toggles[0]);

    // Only the toggled panel unwraps; the other keeps the default.
    const wrapped = Array.from(container.querySelectorAll("pre")).filter((pre) =>
      pre.className.includes("whitespace-pre-wrap"),
    );
    expect(wrapped.length).toBe(1);
    expect(screen.getByRole("button", { name: "Enable word wrap" })).toBeInTheDocument();
  });
});

describe("ToolCard registered views", () => {
  it("uses a registered view's title and body in place of the generic ones", () => {
    // WHY: tool-specific renderers plug in through toolViews.ts without
    // touching ToolCard; each part replaces only its generic counterpart.
    registeredViews.set("fake_plan", {
      title: () => ({ verb: "Started 2 of 2", body: "Fetch open PRs" }),
      Body: ({ call }) => createElement("div", null, `Checklist for ${call.name}`),
    });
    const { container } = renderCard({
      name: "fake_plan",
      arguments: { done: "Find workflow" },
      output: "Task plan updated (2 tasks).",
      state: "output-available",
    });
    expect(screen.getByText("Started 2 of 2")).toBeInTheDocument();
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("Checklist for fake_plan")).toBeInTheDocument();
    expect(screen.queryByText("Input")).toBeNull();
    // The raw payload stays one switch away from a view's body.
    showView("Raw");
    expect(screen.getByText("Input")).toBeInTheDocument();
  });

  it("shows an edit's line counts after its title and its diff behind a Diff | Raw switch", async () => {
    await registerRealView("Edit");
    const { container } = renderCard({
      name: "Edit",
      arguments: {
        file_path: "/repo/config.py",
        old_string: "TIMEOUT = 30\nRETRIES = 3",
        new_string: "TIMEOUT = 60",
      },
      output: "The file /repo/config.py has been updated successfully.",
      state: "output-available",
    });
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    const counts = screen.getByTestId("tool-line-counts");
    expect(trigger.textContent).toBe("Edit /repo/config.py+1 -2");
    expect(within(counts).getByText("+1")).toHaveClass("text-green-600");
    expect(within(counts).getByText("-2")).toHaveClass("text-red-600");

    fireEvent.click(trigger);
    const body = container.querySelector<HTMLElement>('[data-slot="collapsible-content"]')!;
    expect(screen.getByRole("tab", { name: "Diff" })).toHaveAttribute("aria-selected", "true");
    // The Diff tab names the view; the panel is titled with the file and its counts instead.
    expect(screen.getAllByText("Diff")).toHaveLength(1);
    const added = within(body).getByText("+1");
    expect(added).toHaveClass("text-green-600");
    expect(within(body).getByText("-2")).toHaveClass("text-red-600");
    expect(added.parentElement?.textContent).toBe("/repo/config.py+1-2");
    expect(container.textContent).toContain("-TIMEOUT = 30");
    expect(container.textContent).toContain("+TIMEOUT = 60");
    expect(screen.queryByText("Input")).toBeNull();
    showView("Raw");
    expect(screen.getByText("Input")).toBeInTheDocument();
    showView("Diff");
    expect(container.textContent).toContain("-RETRIES = 3");
  });

  it("gives a replace-all edit no row counts and labels its panel's as each match's", async () => {
    await registerRealView("Edit");
    const { container } = renderCard({
      name: "Edit",
      arguments: { file_path: "/repo/a.py", old_string: "x", new_string: "y", replace_all: true },
      output: "The file /repo/a.py has been updated. All occurrences were successfully replaced.",
      state: "output-available",
    });
    expect(screen.queryByTestId("tool-line-counts")).toBeNull();
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("+1").parentElement?.textContent).toBe("/repo/a.py (each match)+1-1");
  });

  it.each([
    {
      producer: "Codex declined patch",
      name: "apply_patch",
      args: { changes: [{ path: "/repo/app.py", kind: { type: "update" }, diff: "@@\n-a\n+b\n" }] },
      output: "Error: File change ended with status declined.\nupdate /repo/app.py",
      error: "Error: File change ended with status declined.",
      diffText: "+b",
    },
    {
      producer: "OpenCode failed edit",
      name: "edit",
      args: { filePath: "a.py", oldString: "x", newString: "y" },
      output: "[error] Error: oldString not found in content",
      error: "Error: oldString not found in content",
      diffText: "+y",
    },
    {
      producer: "SDK Pi failed edit",
      name: "edit",
      args: { path: "a.py", oldText: "x", newText: "y" },
      output: JSON.stringify({
        content: [{ type: "text", text: "Could not find the exact text in a.py." }],
        details: {},
        isError: true,
      }),
      error: "Could not find the exact text in a.py.",
      diffText: "+y",
    },
  ])(
    "shows a $producer's error only inside its expanded diff",
    async ({ name, args, output, error, diffText }) => {
      await registerRealView(name);
      const { container } = renderCard({
        name,
        arguments: args,
        output,
        state: "output-available",
      });
      expect(screen.queryByText(error)).toBeNull();
      expect(screen.queryByTestId("tool-line-counts")).toBeNull();
      fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
      // The expanded body repeats the error in a box, then labels the diff as not applied.
      expect(screen.getByTestId("tool-error-box")).toHaveTextContent(error);
      expect(screen.getByText("Requested change, not applied:")).toBeInTheDocument();
      expect(container.textContent).toContain(diffText);
    },
  );

  it.each([
    ["Permission denied: /repo/a.py", "Permission denied: /repo/a.py"],
    ["  ", "The edit failed with no output."],
  ])(
    "shows a failed edit's unrecognized output %j as its error above the diff",
    async (output, shown) => {
      await registerRealView("Edit");
      const { container } = renderCard({
        name: "Edit",
        arguments: { file_path: "/repo/a.py", old_string: "x", new_string: "y" },
        output,
        state: "output-error",
      });
      fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
      expect(screen.getByTestId("tool-error-box").textContent).toBe(shown);
      expect(container.textContent).toContain("+y");
    },
  );

  it("shows output that does not confirm the edit above its diff, with no counts", async () => {
    await registerRealView("Edit");
    const output = "Error reading /repo/missing.py: [Errno 2] No such file or directory";
    const { container } = renderCard({
      name: "Edit",
      arguments: { file_path: "/repo/missing.py", old_string: "x", new_string: "y" },
      output,
      state: "output-available",
    });
    expect(screen.queryByTestId("tool-line-counts")).toBeNull();
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    const text = container.textContent ?? "";
    expect(text).toContain(output);
    expect(text.indexOf(output)).toBeLessThan(text.indexOf("+y"));
    expect(
      screen.getByText("Requested change; the output does not confirm it applied:"),
    ).toBeInTheDocument();
  });

  it.each([
    ["input-available", "Waiting for output"],
    ["cancelled", "Tool was cancelled before output arrived."],
    ["output-error", "Tool did not return output before the response failed."],
  ] as const)("keeps a %s edit's state under its diff", async (state, message) => {
    await registerRealView("Edit");
    const { container } = renderCard({
      name: "Edit",
      arguments: { file_path: "/repo/a.py", old_string: "a = 1", new_string: "a = 2" },
      output: null,
      state,
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(container.textContent).toContain("+a = 2");
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByText("No diff recorded for this edit.")).toBeNull();
  });

  it("shows a finished edit's output when its arguments give no diff", async () => {
    await registerRealView("apply_patch");
    const files = Array.from({ length: 100 }, (_, index) => `M src/file${index}.py`);
    const { container } = renderCard({
      name: "apply_patch",
      arguments: {},
      output: `Success. Updated the following files:\n${files.join("\n")}`,
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("No diff recorded for this edit.")).toBeInTheDocument();
    expect(container.textContent).toContain("M src/file0.py");
    // Long output keeps the generic preview, with the rest behind Expand.
    expect(container.textContent).not.toContain("M src/file99.py");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(container.textContent).toContain("M src/file99.py");
  });

  it.each([
    ["output-available", "Error: String to replace not found in file."],
    ["output-error", "Permission denied: /repo/a.py"],
  ] as const)("shows a %s edit's error when its arguments give no diff", async (state, output) => {
    await registerRealView("Edit");
    const { container } = renderCard({
      name: "Edit",
      arguments: { file_path: "/repo/a.py" },
      output,
      state,
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByTestId("tool-error-box").textContent).toBe(output);
    expect(screen.getByText("No diff recorded for this edit.")).toBeInTheDocument();
    expect(screen.queryByText("Output")).toBeNull();
  });

  it("keeps a long diff behind the output preview until expanded", async () => {
    await registerRealView("Edit");
    const lines = Array.from({ length: 100 }, (_, index) => `line${index}`);
    const { container } = renderCard({
      name: "Edit",
      arguments: { file_path: "/repo/a.py", old_string: "", new_string: lines.join("\n") },
      output: "The file /repo/a.py has been updated successfully.",
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(container.textContent).toContain("+line0");
    expect(container.textContent).not.toContain("+line99");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(container.textContent).toContain("+line99");
  });

  it("says a finished edit without output or a diff has no diff", async () => {
    await registerRealView("Edit");
    const { container } = renderCard({
      name: "Edit",
      arguments: { file_path: "/repo/a.py" },
      output: null,
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("No diff recorded for this edit.")).toBeInTheDocument();
  });

  it.each([
    ["input-available", "Waiting for output"],
    ["cancelled", "Tool was cancelled before output arrived."],
  ] as const)(
    "does not call a %s edit's incomplete arguments a missing diff",
    async (state, shown) => {
      await registerRealView("Edit");
      const { container } = renderCard({
        name: "Edit",
        arguments: { file_path: "/repo/a.py" },
        output: null,
        state,
      });
      fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
      expect(screen.queryByText("No diff recorded for this edit.")).toBeNull();
      expect(screen.getByText(shown)).toBeInTheDocument();
    },
  );

  it("falls back to the generic parts a view leaves out", () => {
    registeredViews.set("fake_plan", { lineCounts: () => null });
    const { container } = renderCard({
      name: "fake_plan",
      arguments: { step: "one" },
      output: "ok",
      state: "output-available",
    });
    expect(screen.getByText("Fake plan")).toBeInTheDocument();
    expect(screen.queryByTestId("tool-line-counts")).toBeNull();
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    expect(screen.getByText("Input")).toBeInTheDocument();
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
    expect(trigger.textContent).toBe("pnpm install");

    expand(container);
    const terminal = screen.getByTestId("shell-tool-terminal");
    expect(terminal.textContent).toContain("/repo/app");
    // The `$` prompt is CSS-only, so the text is the bare command.
    expect(terminal.textContent).toContain("pnpm install");
    expect(terminal.textContent).not.toContain("zsh -lc");
    expect(terminal.textContent).toContain("Done in 1.2s");
    expect(screen.queryByText("Input")).toBeNull();
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
    // The terminal keeps its own copy control, named for the command's output.
    expect(screen.getByRole("button", { name: "Copy output" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Raw" }));
    expect(screen.getByText("Input")).toBeInTheDocument();
    expect(screen.getByText("Result")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy input" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy result" })).toBeInTheDocument();
    expect(screen.queryByText("Parameters")).toBeNull();
    expect(container.textContent).toContain("\\u001b[1ma.txt\\u001b[0m");
  });

  it.each([
    { name: "shell", output: "missing\n[exit code: 1]", detail: "exit 1" },
    {
      name: "sys_os_shell",
      output: JSON.stringify({ stdout: "", stderr: "", exit_code: null, timed_out: true }),
      detail: "timed out",
    },
    {
      name: "Bash",
      output: "<tool_use_error>Permission to use Bash was denied.</tool_use_error>",
      detail: "Permission to use Bash was denied.",
    },
  ])("shows a $name failure only inside the expanded terminal", ({ name, output, detail }) => {
    const { container } = renderCard({
      name,
      arguments: { command: "make" },
      output,
      state: "output-available",
    });
    expect(container).not.toHaveTextContent(detail);
    expand(container);
    expect(screen.getByTestId("shell-tool-terminal")).toHaveTextContent(detail);
    expand(container);
    expect(container).not.toHaveTextContent(detail);
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

  it("shows a multi-line script's first line and line count in the row", () => {
    // WHY: newlines collapsed into spaces made separate commands read as one.
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "git diff --check\ngit add a \\\n  b\ngit status" },
      output: "",
      state: "output-available",
    });
    const trigger = container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!;
    expect(trigger.textContent).toBe("git diff --check +3 lines");
  });
});

describe("ToolCard shell terminal theme", () => {
  afterEach(() => writeTerminalThemeMode("auto"));

  function renderExpandedShell() {
    const { container } = renderCard({
      name: "Bash",
      arguments: { command: "ls" },
      output: "README.md",
      state: "output-available",
    });
    fireEvent.click(container.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]')!);
    return screen.getByTestId("shell-tool-terminal");
  }

  it("keeps the app palette when the terminal theme follows the app", () => {
    const terminal = renderExpandedShell();
    expect(terminal).toHaveAttribute("data-terminal-theme", "light");
    expect(terminal.style.getPropertyValue("--background")).toBe("");
  });

  it("repaints the block when the terminal theme pins the other scheme", () => {
    // WHY: Settings > Terminal theme applies to terminal-styled shell output,
    // not only to the terminal pane, and a change applies live.
    writeTerminalThemeMode("dark");
    const terminal = renderExpandedShell();
    expect(terminal).toHaveAttribute("data-terminal-theme", "dark");
    expect(terminal.style.getPropertyValue("--background")).toBe("#0a0a0a");
    // Selected text keeps the terminal's light text, not the light app's dark one.
    expect(terminal.style.getPropertyValue("--selection-foreground")).toBe("#e4e4e7");

    act(() => writeTerminalThemeMode("light"));
    expect(terminal).toHaveAttribute("data-terminal-theme", "light");
    expect(terminal.style.getPropertyValue("--background")).toBe("");
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
    expect(screen.getByText("Alpha tool")).toBeInTheDocument();
    expect(screen.getByText("Beta tool")).toBeInTheDocument();
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

import { describe, expect, it } from "vitest";
import { absoluteTime } from "./relativeTime";
import { type ToolCall, summarizeToolResult } from "./toolResultSummary";

function summarize(name: string, output: string | null, args: Record<string, unknown> = {}) {
  const call: ToolCall = { name, args, output, state: "output-available" };
  return summarizeToolResult(call);
}

describe("summarizeToolResult", () => {
  it("has no line while running or before output arrives", () => {
    expect(
      summarizeToolResult({ name: "my_tool", args: {}, output: null, state: "input-available" }),
    ).toBeNull();
    expect(summarize("my_tool", null)).toBeNull();
  });

  describe("shell tools", () => {
    it("stays quiet on success; the terminal panel already shows the output", () => {
      expect(summarize("Bash", "README.md", { command: "ls" })).toBeNull();
      expect(summarize("shell", "ok\n[exit code: 0]", { command: "make" })).toBeNull();
    });

    it("reports a failing exit code with the output's last line", () => {
      expect(
        summarize("shell", "collecting...\nFAILED tests/test_a.py::test_x\n[exit code: 1]", {
          command: "pytest",
        }),
      ).toEqual({ text: "Exit code 1: FAILED tests/test_a.py::test_x", isError: true });
    });

    it("reports a failure from a shell format that carries no exit code", () => {
      // WHY: Claude's Bash output has no exit code, so its failures show only in the text.
      expect(
        summarize("Bash", "<tool_use_error>Permission to use Bash was denied.</tool_use_error>", {
          command: "rm -rf build",
        }),
      ).toEqual({ text: "Permission to use Bash was denied.", isError: true });
      expect(summarize("sys_os_shell", '{"error": "command blocked"}', { command: "x" })).toEqual({
        text: "command blocked",
        isError: true,
      });
    });

    it("reports a sys_os_shell timeout", () => {
      const output = JSON.stringify({ stdout: "", stderr: "", exit_code: null, timed_out: true });
      expect(summarize("sys_os_shell", output, { command: "sleep 99" })).toEqual({
        text: "Timed out",
        isError: true,
      });
    });
  });

  describe("file reads", () => {
    it("counts the numbered lines of a Claude Read, ignoring appended reminders", () => {
      const output =
        "     1\timport a\n     2\timport b\n     3\t\n\n<system-reminder>x</system-reminder>";
      expect(summarize("Read", output, { file_path: "a.py" })).toEqual({
        text: "3 lines",
        isError: false,
      });
    });

    it("counts the content lines of a sys_os_read result", () => {
      const output = JSON.stringify({ path: "a.txt", content: "one\ntwo\n" });
      expect(summarize("mcp__omnigent__sys_os_read", output, { path: "a.txt" })?.text).toBe(
        "2 lines",
      );
      expect(summarize("sys_os_read", JSON.stringify({ path: "e", content: "" }))?.text).toBe(
        "Empty file",
      );
      expect(summarize("sys_os_read", JSON.stringify({ path: "e", content: "\n" }))?.text).toBe(
        "Empty file",
      );
      const emptyRead = "<system-reminder>Warning: the file exists but is empty.</system-reminder>";
      expect(summarize("Read", emptyRead, { file_path: "e" })?.text).toBe("Empty file");
    });
  });

  it("stays quiet on a successful edit and surfaces a failed one", () => {
    expect(summarize("Edit", "The file a.py has been updated successfully.")).toBeNull();
    expect(
      summarize("Edit", "<tool_use_error>String to replace not found in file.</tool_use_error>"),
    ).toEqual({ text: "String to replace not found in file.", isError: true });
  });

  it("uses Claude's own count for file searches, else counts result lines", () => {
    expect(summarize("Grep", "Found 3 files\na.ts\nb.ts\nc.ts")?.text).toBe("Found 3 files");
    expect(summarize("Glob", "No files found")?.text).toBe("No files found");
    expect(summarize("glob", "a.ts\nb.ts\n")?.text).toBe("2 results");
  });

  describe("other tools", () => {
    it("shows the first line of a text result", () => {
      expect(summarize("manage_todo_list", "Task plan updated (2 tasks).")).toEqual({
        text: "Task plan updated (2 tasks).",
        isError: false,
      });
      expect(summarize("my_tool", "  \n")?.text).toBe("No output");
    });

    it("shows a record's title and state from an MCP content-block result", () => {
      const issue = {
        id: "OMNI-12243",
        title: "Claude mod replaces tmux paste",
        state: "In Review",
      };
      const output = JSON.stringify([{ type: "text", text: JSON.stringify(issue) }]);
      expect(summarize("mcp__linear__get_issue", output)?.text).toBe(
        "Claude mod replaces tmux paste · In Review",
      );
    });

    it("falls back to the first two scalar fields, with times in local time", () => {
      const output = JSON.stringify({
        task_id: "st_8f21c0",
        next_run_at: "2026-10-27T09:00:00-07:00",
        enabled: true,
      });
      expect(summarize("sys_scheduled_task_create", output)?.text).toBe(
        `Task ID st_8f21c0 · Next run at ${absoluteTime(Date.parse("2026-10-27T16:00:00Z"))}`,
      );
    });

    it("counts array results", () => {
      expect(summarize("list_prs", "[]")?.text).toBe("No results");
      expect(summarize("list_prs", "[1, 2, 3]")?.text).toBe("3 results");
    });

    it("marks a reported failure as an error", () => {
      expect(summarize("sys_os_write", '{"error": "permission denied"}')).toEqual({
        text: "permission denied",
        isError: true,
      });
      expect(
        summarize("mcp__github__create_pull_request", "Error: 422 Validation Failed\nmore detail"),
      ).toEqual({ text: "Error: 422 Validation Failed", isError: true });
      expect(
        summarize("github__create_pull_request", '{"result": "Error: 422 Validation Failed"}'),
      ).toEqual({ text: "Error: 422 Validation Failed", isError: true });
    });
  });
});

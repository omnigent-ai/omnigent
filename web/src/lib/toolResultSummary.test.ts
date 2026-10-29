import { describe, expect, it } from "vitest";
import { absoluteTime } from "./relativeTime";
import {
  type ToolCall,
  editApplied,
  summarizeFileEdit,
  summarizeToolResult,
} from "./toolResultSummary";

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
      // "[error] <message>" is an edit tool's failure form, not a shell's.
      expect(summarize("Bash", "[error] recovered cache entry", { command: "cat log" })).toBeNull();
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
      const piFailure = {
        content: [{ type: "text", text: "Command exited with code 1" }],
        details: { truncation: null },
        isError: true,
      };
      expect(summarize("bash", JSON.stringify(piFailure))).toEqual({
        text: "Command exited with code 1",
        isError: true,
      });
    });
  });
});

describe("summarizeFileEdit", () => {
  const edit = (output: string | null, overrides: Partial<ToolCall> = {}): ToolCall => ({
    name: "Edit",
    args: { file_path: "a.py", old_string: "x = 1\ny = 2\nz = 3", new_string: "x = 10" },
    output,
    state: "output-available",
    ...overrides,
  });

  it("reports added and removed line counts, each number in its diff color", () => {
    expect(summarizeFileEdit(edit("The file a.py has been updated successfully."))).toEqual({
      text: "Updated with 1 addition and 3 removals",
      isError: false,
      segments: [
        { text: "Updated with " },
        { text: "1", tone: "added" },
        { text: " addition" },
        { text: " and " },
        { text: "3", tone: "removed" },
        { text: " removals" },
      ],
    });
  });

  it("leaves out a zero count and names the file count of a multi-file patch", () => {
    const call: ToolCall = {
      name: "apply_patch",
      args: {
        changes: [
          { path: "a.py", kind: { type: "add" }, diff: "one\ntwo\n" },
          { path: "b.py", kind: { type: "update" }, diff: "@@ -1 +1,2 @@\n keep\n+three\n" },
        ],
      },
      output: "File change applied.\nadd a.py\nupdate b.py",
      state: "output-available",
    };
    expect(summarizeFileEdit(call)?.text).toBe("Updated 2 files with 3 additions");
    const wrapped = JSON.stringify([{ type: "text", text: call.output }]);
    expect(summarizeFileEdit({ ...call, output: wrapped })?.text).toBe(
      "Updated 2 files with 3 additions",
    );
  });

  it("keeps the error line for a failed edit", () => {
    expect(
      summarizeFileEdit(edit("<tool_use_error>String to replace not found.</tool_use_error>")),
    ).toEqual({ text: "String to replace not found.", isError: true });
    expect(
      summarizeFileEdit({
        name: "apply_patch",
        args: { changes: [{ path: "a.py", kind: { type: "add" }, diff: "x\n" }] },
        output: "Error: File change ended with status declined.\nadd a.py",
        state: "output-available",
      }),
    ).toEqual({ text: "Error: File change ended with status declined.", isError: true });
    expect(
      summarizeFileEdit({
        name: "edit",
        args: { filePath: "a.py", oldString: "x", newString: "y" },
        output: "[error] Error: oldString not found in content",
        state: "output-available",
      }),
    ).toEqual({ text: "Error: oldString not found in content", isError: true });
    const piFailure = { content: [{ type: "text", text: "Could not find the text in a.py." }] };
    expect(
      summarizeFileEdit({
        name: "edit",
        args: { path: "a.py", oldText: "x", newText: "y" },
        output: JSON.stringify({ ...piFailure, isError: true }),
        state: "output-available",
      }),
    ).toEqual({ text: "Could not find the text in a.py.", isError: true });
  });

  const patchArgs = {
    changes: [{ path: "/repo/app.py", kind: { type: "update" }, diff: "@@\n-a\n+b\n" }],
  };
  const replaceArgs = {
    path: "a.py",
    oldText: "x",
    newText: "y",
    old_string: "x",
    new_string: "y",
  };
  it.each([
    { name: "apply_patch", args: patchArgs, output: "update /repo/app.py" },
    { name: "mcp__omnigent__apply_patch", args: patchArgs, output: "update /repo/app.py" },
    {
      name: "Edit",
      args: replaceArgs,
      output: "Error reading /repo/missing.py: [Errno 2] No such file or directory",
    },
    { name: "edit", args: replaceArgs, output: "Could not find the exact text in a.py." },
  ])("shows no counts for $name output that does not say it applied: $output", (overrides) => {
    expect(summarizeFileEdit(edit(overrides.output, overrides))).toBeNull();
  });

  it("counts a replace-all edit per match, since its output does not say how many", () => {
    const output = "The file a.py has been updated. All occurrences were successfully replaced.";
    const replaceAll = edit(output, {
      args: { file_path: "a.py", old_string: "x", new_string: "y", replace_all: true },
    });
    expect(summarizeFileEdit(replaceAll)?.text).toBe(
      "Updated each match with 1 addition and 1 removal",
    );
    const batch = edit("Applied 2 edits to a.py:", {
      name: "MultiEdit",
      args: {
        file_path: "a.py",
        edits: [
          { old_string: "x", new_string: "y", replace_all: true },
          { old_string: "z", new_string: "w" },
        ],
      },
    });
    expect(summarizeFileEdit(batch)).toBeNull();
  });

  it("shows no counts for an edit too large to compare line by line", () => {
    const shared = Array.from({ length: 500 }, (_, index) => `line ${index}`);
    const large = edit("The file a.py has been updated successfully.", {
      args: {
        file_path: "a.py",
        old_string: [...shared, "old end"].join("\n"),
        new_string: ["new start", ...shared].join("\n"),
      },
    });
    expect(summarizeFileEdit(large)).toBeNull();
  });

  it("shows no counts for a patch that only renames a file", () => {
    const call: ToolCall = {
      name: "apply_patch",
      args: {
        changes: [{ path: "a.py", kind: { type: "update", move_path: "b.py" }, diff: "" }],
      },
      output: "File change applied.\nupdate a.py",
      state: "output-available",
    };
    expect(summarizeFileEdit(call)).toBeNull();
  });

  it("shows no counts while running, after a cancelled or failed turn, or without a change", () => {
    expect(summarizeFileEdit(edit(null, { state: "input-available" }))).toBeNull();
    expect(summarizeFileEdit(edit(null, { state: "cancelled" }))).toBeNull();
    expect(summarizeFileEdit(edit("done", { state: "output-error" }))).toBeNull();
    expect(summarizeFileEdit(edit("done", { args: { file_path: "a.py" } }))).toBeNull();
  });
});

describe("editApplied", () => {
  it.each([
    ["Edit", "The file /repo/a.py has been updated successfully."],
    ["Edit", "The file /repo/a.py has been updated. All occurrences were successfully replaced."],
    ["Edit", "Replaced 1 occurrence(s) in /repo/a.py"],
    ["MultiEdit", "Applied 2 edits to /repo/a.py:\n1. Replaced ..."],
    ["edit", "Successfully replaced 1 block(s) in a.py."],
    ["edit", "Successfully replaced text in a.py."],
    ["edit", "Edit applied successfully.\n\nLSP errors detected"],
    ["edit", "The file: /repo/a.py has been updated."],
    ["edit", "Created new file: /repo/a.py with provided content."],
    ["mcp__omnigent__sys_os_edit", '{"path": "/repo/a.py", "replacements": 1, "bytes_written": 9}'],
    ["apply_patch", "File change applied.\nupdate /repo/a.py"],
    // SDK Pi stores its own tools' results whole.
    [
      "edit",
      `{"content": [{"type": "text", "text": "Successfully replaced text in a.py."}], "details": {"diff": "-1 x\\n+1 y", "firstChangedLine": 1}}`,
    ],
  ])("accepts %s output %j", (name, output) => {
    expect(editApplied(name, output)).toBe(true);
  });

  it.each([
    ["Edit", "ok"],
    ["edit", "Could not find the exact text in a.py."],
    ["sys_os_edit", '{"path": "/repo/a.py", "replacements": 0}'],
    ["sys_os_edit", "Replaced 1 occurrence(s) in /repo/a.py"],
    ["apply_patch", "update /repo/a.py"],
    ["apply_patch", "Successfully replaced text in a.py."],
    [
      "edit",
      '{"content": [{"type": "text", "text": "Successfully replaced text in a.py."}], "isError": true}',
    ],
    ["edit", '{"content": [{"type": "text", "text": "Could not find the exact text in a.py."}]}'],
  ])("rejects %s output %j", (name, output) => {
    expect(editApplied(name, output)).toBe(false);
  });
});

import { describe, expect, it } from "vitest";
import { type ToolCall, editApplied, fileEditLineCounts } from "./toolResultSummary";

const edit = (output: string | null, overrides: Partial<ToolCall> = {}): ToolCall => ({
  name: "Edit",
  args: { file_path: "a.py", old_string: "x = 1\ny = 2\nz = 3", new_string: "x = 10" },
  output,
  state: "output-available",
  ...overrides,
});

describe("fileEditLineCounts", () => {
  it("counts an applied edit's added and removed lines", () => {
    expect(fileEditLineCounts(edit("The file a.py has been updated successfully."))).toEqual({
      additions: 1,
      removals: 3,
    });
  });

  it("totals a multi-file patch, wrapped or not", () => {
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
    expect(fileEditLineCounts(call)).toEqual({ additions: 3, removals: 0 });
    const wrapped = JSON.stringify([{ type: "text", text: call.output }]);
    expect(fileEditLineCounts({ ...call, output: wrapped })).toEqual({ additions: 3, removals: 0 });
  });

  it.each([
    "<tool_use_error>String to replace not found.</tool_use_error>",
    "[error] Error: oldString not found in content",
    JSON.stringify({
      content: [{ type: "text", text: "Could not find the text." }],
      isError: true,
    }),
    '{"error": "permission denied"}',
  ])("has none for a failed edit's output %j", (output) => {
    expect(fileEditLineCounts(edit(output))).toBeNull();
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
  ])("has none for $name output that does not say it applied: $output", (overrides) => {
    expect(fileEditLineCounts(edit(overrides.output, overrides))).toBeNull();
  });

  it("has none for a replace-all edit, whose output does not say how many matched", () => {
    const output = "The file a.py has been updated. All occurrences were successfully replaced.";
    const replaceAll = edit(output, {
      args: { file_path: "a.py", old_string: "x", new_string: "y", replace_all: true },
    });
    expect(fileEditLineCounts(replaceAll)).toBeNull();
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
    expect(fileEditLineCounts(batch)).toBeNull();
  });

  it("has none for an edit too large to compare line by line", () => {
    const shared = Array.from({ length: 500 }, (_, index) => `line ${index}`);
    const large = edit("The file a.py has been updated successfully.", {
      args: {
        file_path: "a.py",
        old_string: [...shared, "old end"].join("\n"),
        new_string: ["new start", ...shared].join("\n"),
      },
    });
    expect(fileEditLineCounts(large)).toBeNull();
  });

  it("has none for a patch that only renames a file", () => {
    const call: ToolCall = {
      name: "apply_patch",
      args: {
        changes: [{ path: "a.py", kind: { type: "update", move_path: "b.py" }, diff: "" }],
      },
      output: "File change applied.\nupdate a.py",
      state: "output-available",
    };
    expect(fileEditLineCounts(call)).toBeNull();
  });

  it("has none while running, after a cancelled or failed turn, or without a change", () => {
    expect(fileEditLineCounts(edit(null, { state: "input-available" }))).toBeNull();
    expect(fileEditLineCounts(edit(null, { state: "cancelled" }))).toBeNull();
    expect(fileEditLineCounts(edit("done", { state: "output-error" }))).toBeNull();
    expect(fileEditLineCounts(edit("done", { args: { file_path: "a.py" } }))).toBeNull();
  });
});

describe("fileEditLineCounts for full-file writes", () => {
  const write = (name: string, args: Record<string, unknown>, output: string): ToolCall => ({
    name,
    args,
    output,
    state: "output-available",
  });

  it("counts every line of a write that created its file", () => {
    const args = { file_path: "/repo/a.py", content: "one\ntwo\nthree\n" };
    expect(
      fileEditLineCounts(write("Write", args, "File created successfully at: /repo/a.py")),
    ).toEqual({ additions: 3, removals: 0 });
    const record = '{"path": "/repo/a.py", "bytes_written": 14, "created": true}';
    expect(
      fileEditLineCounts(write("sys_os_write", { path: "/repo/a.py", content: "x\n" }, record)),
    ).toEqual({ additions: 1, removals: 0 });
  });

  it.each([
    ["Write", "The file /repo/a.py has been updated successfully."],
    ["sys_os_write", '{"path": "/repo/a.py", "bytes_written": 2, "created": false}'],
    ["write", "Successfully wrote 2 bytes to /repo/a.py"],
    ["Write", "<tool_use_error>File has not been read yet.</tool_use_error>"],
    ["sys_os_write", '{"error": "permission denied"}'],
  ])("has none for a %s that did not say it created its file: %j", (name, output) => {
    const args = { file_path: "/repo/a.py", path: "/repo/a.py", content: "x\n" };
    expect(fileEditLineCounts(write(name, args, output))).toBeNull();
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
    ["Write", "File created successfully at: /repo/a.py (file state is current in your context)"],
    ["mcp__omnigent__sys_os_write", '{"path": "/repo/a.py", "bytes_written": 9, "created": false}'],
    ["write", "Successfully wrote 9 bytes to /repo/a.py"],
    ["write", "Wrote file successfully."],
    ["write", "Created file successfully: /repo/a.py"],
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
    ["sys_os_write", '{"error": "permission denied"}'],
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

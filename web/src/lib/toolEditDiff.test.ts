import { describe, expect, it } from "vitest";
import { getFileEditDiff } from "./toolEditDiff";

describe("getFileEditDiff", () => {
  it("derives removed/added lines from sys_os_edit oldText/newText", () => {
    const diff = getFileEditDiff(
      "sys_os_edit",
      { path: "f.py", oldText: "timeout = 30\n", newText: "timeout = 60\n" },
      null,
    );
    expect(diff).toContain("-timeout = 30");
    expect(diff).toContain("+timeout = 60");
  });

  it("keeps shared context lines as context, not churn", () => {
    const diff = getFileEditDiff(
      "sys_os_edit",
      {
        path: "f.py",
        oldText: "keep_a\nkeep_b\nold_value\nkeep_c\n",
        newText: "keep_a\nkeep_b\nnew_value\nkeep_c\n",
      },
      null,
    );
    expect(diff).toContain(" keep_b");
    expect(diff).toContain("-old_value");
    expect(diff).toContain("+new_value");
    expect(diff).toContain(" keep_c");
    expect(diff).not.toContain("-keep_b");
  });

  it("renders every entry of an edits array", () => {
    const diff = getFileEditDiff(
      "sys_os_edit",
      {
        path: "f.py",
        edits: [
          { oldText: "alpha\n", newText: "beta\n" },
          { oldText: "one\n", newText: "two\n" },
        ],
      },
      null,
    );
    expect(diff).toContain("-alpha");
    expect(diff).toContain("+beta");
    expect(diff).toContain("-one");
    expect(diff).toContain("+two");
  });

  it("accepts Claude Code Edit's old_string/new_string keys", () => {
    const diff = getFileEditDiff(
      "Edit",
      { file_path: "/w/f.py", old_string: "x = 1\n", new_string: "x = 2\n" },
      null,
    );
    expect(diff).toContain("-x = 1");
    expect(diff).toContain("+x = 2");
  });

  it("reads a write tool's diff from the JSON result", () => {
    const output = JSON.stringify({
      path: "/w/config.py",
      bytes_written: 20,
      created: false,
      diff: "--- /w/config.py\n+++ /w/config.py\n@@ -1 +1 @@\n-old\n+new\n",
    });
    const diff = getFileEditDiff("sys_os_write", { path: "config.py", content: "new\n" }, output);
    expect(diff).toContain("-old");
    expect(diff).toContain("+new");
  });

  it("returns null for a write that created a new file (no diff field)", () => {
    const output = JSON.stringify({ path: "/w/new.txt", bytes_written: 4, created: true });
    expect(
      getFileEditDiff("sys_os_write", { path: "new.txt", content: "hi\n" }, output),
    ).toBeNull();
  });

  it("returns null for non-file tools and malformed shapes", () => {
    expect(getFileEditDiff("sys_os_shell", { command: "ls" }, "out")).toBeNull();
    expect(getFileEditDiff("sys_os_edit", { path: "f.py" }, null)).toBeNull();
    expect(getFileEditDiff("sys_os_write", { path: "f" }, "not json")).toBeNull();
  });

  it("returns null when oldText equals newText", () => {
    expect(
      getFileEditDiff("sys_os_edit", { path: "f", oldText: "same\n", newText: "same\n" }, null),
    ).toBeNull();
  });
});

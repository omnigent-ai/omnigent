import { describe, expect, it } from "vitest";
import { getFileEditDiff } from "./toolEditDiff";

describe("getFileEditDiff", () => {
  it("diffs a Claude Edit's old and new strings line by line", () => {
    const diff = getFileEditDiff("Edit", {
      file_path: "/repo/config.py",
      old_string: "HOST = 'localhost'\nTIMEOUT = 30\nRETRIES = 3\n",
      new_string: "HOST = 'localhost'\nTIMEOUT = 60\nRETRIES = 3\n",
    });
    expect(diff).toEqual({
      files: [
        {
          path: "/repo/config.py",
          kind: "update",
          text: " HOST = 'localhost'\n-TIMEOUT = 30\n+TIMEOUT = 60\n RETRIES = 3",
          additions: 1,
          removals: 1,
        },
      ],
      additions: 1,
      removals: 1,
      countsArePerMatch: false,
      countsAreInexact: false,
    });
  });

  it("does not count unchanged lines between two changes", () => {
    // WHY: trimming only the common prefix and suffix would count "b" as removed and re-added.
    const diff = getFileEditDiff("edit", { path: "x", oldText: "a\nb\nc", newText: "A\nb\nC" });
    expect(diff?.files[0]?.text).toBe("-a\n+A\n b\n-c\n+C");
    expect(diff).toMatchObject({ additions: 2, removals: 2 });
  });

  it("collapses a long unchanged run between changes into a gap", () => {
    const middle = ["1", "2", "3", "4", "5", "6"];
    const diff = getFileEditDiff("Edit", {
      old_string: ["a", ...middle, "z"].join("\n"),
      new_string: ["A", ...middle, "Z"].join("\n"),
    });
    expect(diff?.files[0]?.text).toBe("-a\n+A\n 1\n 2\n@@\n 5\n 6\n-z\n+Z");
  });

  it("joins each edit of a MultiEdit or sys_os_edit call as its own hunk", () => {
    const edits = [
      { old_string: "one", new_string: "uno" },
      { old_string: "two", new_string: "two" },
      { old_string: "three\n", new_string: "three\nfour\n" },
    ];
    expect(getFileEditDiff("MultiEdit", { file_path: "a.txt", edits })).toMatchObject({
      files: [{ path: "a.txt", text: "@@\n-one\n+uno\n@@\n three\n+four" }],
      additions: 2,
      removals: 1,
    });
    const omnigentEdits = [{ oldText: "x = 1", newText: "x = 2" }];
    expect(
      getFileEditDiff("mcp__omnigent__sys_os_edit", { path: "a.py", edits: omnigentEdits }),
    ).toMatchObject({
      files: [{ path: "a.py", text: "-x = 1\n+x = 2" }],
      additions: 1,
      removals: 1,
    });
  });

  it("counts each edit of a batch on its own, not the file's net change", () => {
    const edits = [
      { old_string: "a", new_string: "a\nb" },
      { old_string: "b", new_string: "c" },
    ];
    expect(getFileEditDiff("MultiEdit", { file_path: "a.txt", edits })).toMatchObject({
      additions: 2,
      removals: 1,
    });
  });

  it("reads OpenCode's camel-case keys and counts a pure insertion", () => {
    expect(
      getFileEditDiff("edit", { filePath: "a.ts", oldString: "", newString: "line 1\nline 2\n" }),
    ).toMatchObject({ files: [{ path: "a.ts", text: "+line 1\n+line 2" }], additions: 2 });
  });

  it("shows an edit that only adds or removes a final newline as a blank line", () => {
    expect(getFileEditDiff("Edit", { old_string: "a", new_string: "a\n" })).toMatchObject({
      files: [{ text: " a\n+" }],
      additions: 1,
      removals: 0,
    });
    expect(getFileEditDiff("Edit", { old_string: "a\n", new_string: "a" })).toMatchObject({
      files: [{ text: " a\n-" }],
      additions: 0,
      removals: 1,
    });
    const batch = getFileEditDiff("MultiEdit", {
      edits: [
        { old_string: "x", new_string: "y" },
        { old_string: "end", new_string: "end\n" },
      ],
    });
    expect(batch).toMatchObject({
      files: [{ text: "@@\n-x\n+y\n@@\n end\n+" }],
      additions: 2,
      removals: 1,
    });
  });

  it("shows a change too large to compare line by line as a whole-block replacement", () => {
    const shared = Array.from({ length: 500 }, (_, index) => `line ${index}`);
    const diff = getFileEditDiff("Edit", {
      old_string: [...shared, "old end"].join("\n"),
      new_string: ["new start", ...shared].join("\n"),
    });
    // 501 × 501 lines exceeds the comparison budget, so no shared line is matched.
    expect(diff).toMatchObject({ additions: 501, removals: 501, countsAreInexact: true });
  });

  it("shares the comparison budget across a call's edits", () => {
    const shared = Array.from({ length: 399 }, (_, index) => `line ${index}`);
    const pair = {
      old_string: [...shared, "old end"].join("\n"),
      new_string: ["new start", ...shared].join("\n"),
    };
    // Each 400 × 400 edit fits the budget alone; the second exceeds what the first left.
    const diff = getFileEditDiff("MultiEdit", { file_path: "a.py", edits: [pair, pair] });
    expect(diff).toMatchObject({ additions: 1 + 400, removals: 1 + 400, countsAreInexact: true });
    const fits = getFileEditDiff("MultiEdit", { file_path: "a.py", edits: [pair] });
    expect(fits).toMatchObject({ additions: 1, removals: 1, countsAreInexact: false });
  });

  it("returns null for a no-op, malformed, or non-edit call", () => {
    expect(getFileEditDiff("Edit", { old_string: "same", new_string: "same" })).toBeNull();
    expect(getFileEditDiff("Edit", { file_path: "a.py" })).toBeNull();
    expect(getFileEditDiff("Read", { file_path: "a.py" })).toBeNull();
    expect(getFileEditDiff("mcp__linear__edit", { oldText: "a", newText: "b" })).toBeNull();
  });

  it("splits a Codex apply_patch into one section per file, by change kind", () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "/repo/app.py",
          kind: { type: "update", move_path: null },
          diff: "@@ -1,3 +1,3 @@\n import os\n-DEBUG = True\n+DEBUG = False\n print(os.name)\n",
        },
        { path: "/repo/new.py", kind: { type: "add" }, diff: "print('hi')\nprint('bye')\n" },
        { path: "/repo/old.py", kind: { type: "delete" }, diff: "gone\n" },
      ],
    });
    expect(diff).toMatchObject({ additions: 3, removals: 2 });
    expect(diff?.files).toEqual([
      {
        path: "/repo/app.py",
        kind: "update",
        text: "@@ -1,3 +1,3 @@\n import os\n-DEBUG = True\n+DEBUG = False\n print(os.name)",
        additions: 1,
        removals: 1,
      },
      {
        path: "/repo/new.py",
        kind: "add",
        text: "+print('hi')\n+print('bye')",
        additions: 2,
        removals: 0,
      },
      { path: "/repo/old.py", kind: "delete", text: "-gone", additions: 0, removals: 1 },
    ]);
  });

  it("does not count an update that changes nothing as one of the patch's files", () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        { path: "a.py", kind: { type: "add" }, diff: "x\n" },
        { path: "b.py", kind: { type: "update" }, diff: "--- a/b.py\n+++ b/b.py\n" },
        { path: "c.py", kind: { type: "update" }, diff: "@@ -1 +1 @@\n keep\n" },
        { path: "empty.py", kind: { type: "add" }, diff: "" },
      ],
    });
    expect(diff).toMatchObject({ additions: 1, removals: 0 });
    expect(diff?.files.map((file) => [file.path, file.text])).toEqual([
      ["a.py", "+x"],
      ["empty.py", ""],
    ]);
  });

  it("keeps a patch that only adds or deletes an empty file", () => {
    for (const type of ["add", "delete"]) {
      const diff = getFileEditDiff("apply_patch", {
        changes: [{ path: "pkg/__init__.py", kind: { type }, diff: "" }],
      });
      expect(diff).toMatchObject({
        files: [{ path: "pkg/__init__.py", kind: type, text: "" }],
        additions: 0,
        removals: 0,
      });
    }
  });

  it("names a renamed file's destination, even when its content is unchanged", () => {
    const moved = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "/repo/old.py",
          kind: { type: "update", move_path: "/repo/new.py" },
          diff: "@@ -1 +1 @@\n-a\n+b\n",
        },
      ],
    });
    expect(moved?.files).toEqual([
      {
        path: "/repo/old.py",
        newPath: "/repo/new.py",
        kind: "update",
        text: "@@ -1 +1 @@\n-a\n+b",
        additions: 1,
        removals: 1,
      },
    ]);
    const renamedOnly = getFileEditDiff("apply_patch", {
      changes: [
        { path: "/repo/a.py", kind: { type: "update", move_path: "/repo/b.py" }, diff: "" },
      ],
    });
    expect(renamedOnly).toMatchObject({
      files: [{ path: "/repo/a.py", newPath: "/repo/b.py", text: "" }],
      additions: 0,
      removals: 0,
    });
  });

  it("counts a lone replace-all edit per match, and a batch with one as inexact", () => {
    const perMatch = { countsArePerMatch: true, countsAreInexact: false };
    expect(
      getFileEditDiff("Edit", { old_string: "a", new_string: "b", replace_all: true }),
    ).toMatchObject({ additions: 1, removals: 1, ...perMatch });
    expect(
      getFileEditDiff("edit", { oldString: "a", newString: "b", replaceAll: true }),
    ).toMatchObject(perMatch);
    expect(
      getFileEditDiff("MultiEdit", {
        edits: [
          { old_string: "a", new_string: "b" },
          { old_string: "c", new_string: "d", replace_all: true },
        ],
      }),
    ).toMatchObject({ countsArePerMatch: false, countsAreInexact: true });
    expect(getFileEditDiff("Edit", { old_string: "a", new_string: "b" })).toMatchObject({
      countsArePerMatch: false,
      countsAreInexact: false,
    });
  });

  it('keeps removed "-- comment" lines of an update diff without a hunk header', () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "q.sql",
          kind: { type: "update" },
          diff: "--- a/q.sql\n+++ b/q.sql\n--- old note\n+-- new note\n",
        },
      ],
    });
    expect(diff).toMatchObject({ additions: 1, removals: 1 });
    expect(diff?.files[0]?.text).toBe("--- old note\n+-- new note");
  });

  it("strips only one file header pair from an update diff without a hunk header", () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "notes.txt",
          kind: { type: "update" },
          diff: "--- a/notes.txt\n+++ b/notes.txt\n--- old\n+++ new\n",
        },
      ],
    });
    expect(diff).toMatchObject({ additions: 1, removals: 1 });
    expect(diff?.files[0]?.text).toBe("--- old\n+++ new");
  });

  it("skips an update diff's own file headers when counting", () => {
    const diff = getFileEditDiff("apply_patch", {
      changes: [
        {
          path: "a.txt",
          kind: { type: "update" },
          diff: "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-x\n+y\n",
        },
      ],
    });
    expect(diff).toMatchObject({ files: [{ path: "a.txt" }], additions: 1, removals: 1 });
  });

  it("returns null for an apply_patch without usable changes", () => {
    expect(getFileEditDiff("apply_patch", {})).toBeNull();
    expect(getFileEditDiff("apply_patch", { changes: [{ kind: { type: "add" } }] })).toBeNull();
  });

  describe("full-file writes", () => {
    const content = "def greet(name):\n    return name\n";
    it.each([
      ["Write", { file_path: "/repo/a.py", content }, "File created successfully at: /repo/a.py"],
      [
        "mcp__omnigent__sys_os_write",
        { path: "/repo/a.py", content },
        JSON.stringify([{ type: "text", text: '{"path": "/repo/a.py", "created": true}' }]),
      ],
      ["write", { filePath: "/repo/a.py", content }, "Created file successfully: /repo/a.py"],
    ])("shows a %s that created its file as all added lines", (name, args, output) => {
      expect(getFileEditDiff(name, args, output)).toEqual({
        files: [
          {
            path: "/repo/a.py",
            kind: "add",
            text: "+def greet(name):\n+    return name",
            additions: 2,
            removals: 0,
          },
        ],
        additions: 2,
        removals: 0,
        countsArePerMatch: false,
        countsAreInexact: false,
      });
    });

    it.each([
      ["Write", { file_path: "/repo/a.py", content }, "The file /repo/a.py has been updated."],
      ["sys_os_write", { path: "/repo/a.py", content }, '{"path": "/repo/a.py", "created": false}'],
      ["write", { path: "/repo/a.py", content }, "Successfully wrote 33 bytes to /repo/a.py"],
      ["write", { filePath: "/repo/a.py", content }, "Wrote file successfully."],
      ["Write", { file_path: "/repo/a.py", content }, null],
    ])(
      "shows a %s that may have replaced a file as its content, uncounted",
      (name, args, output) => {
        // WHY: the arguments hold only the new content, so lines it replaced are unknown.
        expect(getFileEditDiff(name, args, output)).toMatchObject({
          files: [
            {
              path: "/repo/a.py",
              kind: "write",
              text: "def greet(name):\n    return name",
              additions: 0,
            },
          ],
          additions: 0,
          removals: 0,
        });
      },
    );

    it("returns null for a write without content", () => {
      expect(getFileEditDiff("Write", { file_path: "/repo/a.py" }, "File created")).toBeNull();
    });
  });
});

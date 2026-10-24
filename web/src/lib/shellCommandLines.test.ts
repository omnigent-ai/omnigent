import { describe, expect, it } from "vitest";
import { oneLineShellCommand, shellCommandStarts } from "./shellCommandLines";

describe("shellCommandStarts", () => {
  it.each([
    ["one command per line", "git diff --check\ngit status --short", [true, true]],
    ["a backslash continuation", "git add a \\\n  b \\\n  c\nls", [true, false, false, true]],
    ["a trailing && or pipe", "cd web &&\n  pnpm test |\n  tail\nls", [true, false, false, true]],
    [
      "a multi-line double-quoted string",
      'git commit -m "title\n\nbody"\nls',
      [true, false, false, true],
    ],
    [
      "a multi-line single-quoted script",
      "python -c '\nimport sys\n'\nls",
      [true, false, false, true],
    ],
    ["an escaped quote inside double quotes", 'echo "a \\" b"\nls', [true, true]],
    ["a heredoc body", "cat > f <<'EOF'\nif x\n  'y\nEOF\nls", [true, false, false, false, true]],
    ["a tab-stripped heredoc", "cat <<-END\n\tbody\n\tEND\nls", [true, false, false, true]],
    ["a here-string, which has no body", "cat <<< hi\nls", [true, true]],
    ["an if block", "if [ -f x ]; then\n  rm x\nfi\nls", [true, false, false, true]],
    ["a for loop", "for f in *.py\ndo\n  echo $f\ndone\nls", [true, false, false, false, true]],
    [
      "a case statement",
      "case $x in\n  a|b) echo ab ;;\n  *) echo other ;;\nesac\nls",
      [true, false, false, false, true],
    ],
    ["a brace group", "{\n  echo a\n} > out\nls", [true, false, false, true]],
    ["a function", "greet() {\n  echo hi\n}\ngreet", [true, false, false, true]],
    ["a multi-line command substitution", "x=$(\n  date\n)\necho $x", [true, false, false, true]],
    [
      "blank lines and comments",
      "# check\n\ngit status # no fi here\nls",
      [true, false, true, true],
    ],
    ["keywords used as arguments", "echo if for done\nls", [true, true]],
  ])("splits %s", (_label, script, expected) => {
    expect(shellCommandStarts(script)).toEqual(expected);
  });

  it("does not treat a quote inside a comment as opening a string", () => {
    expect(shellCommandStarts("ls # don't\npwd")).toEqual([true, true]);
  });

  it("keeps a comment's trailing backslash literal", () => {
    expect(shellCommandStarts("# see \\\nls")).toEqual([true, true]);
  });

  it("reads CRLF line endings", () => {
    expect(shellCommandStarts("cat <<EOF\r\nbody\r\nEOF\r\nls")).toEqual([
      true,
      false,
      false,
      true,
    ]);
  });
});

describe("oneLineShellCommand", () => {
  it("leaves a one-line command as it is", () => {
    expect(oneLineShellCommand("ls -la")).toBe("ls -la");
  });

  it("joins separate commands with a semicolon", () => {
    expect(oneLineShellCommand("git diff --check\ngit status --short\n")).toBe(
      "git diff --check; git status --short",
    );
  });

  it("joins continuation lines with a space and drops the backslash", () => {
    expect(oneLineShellCommand("git add a \\\n  b\ngit commit")).toBe("git add a b; git commit");
  });

  it("does not double an existing separator", () => {
    expect(oneLineShellCommand("make &\nwait;\nls")).toBe("make & wait; ls");
  });

  it("leaves out comment-only lines", () => {
    expect(oneLineShellCommand("# Inspect the tree\ngit status")).toBe("git status");
  });

  it("keeps a comment-only script", () => {
    expect(oneLineShellCommand("# nothing to run\n")).toBe("# nothing to run");
  });
});

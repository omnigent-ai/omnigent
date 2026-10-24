import { describe, expect, it } from "vitest";
import { shellCommandPreview, shellCommandStarts } from "./shellCommandLines";

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
    ["a shift in arithmetic expansion", "echo $((1<<2))\nls\npwd", [true, true, true]],
    ["a shift in an arithmetic command", "(( x <<= 1 ))\nls", [true, true]],
    ["nested parentheses in arithmetic", "echo $(( (1 << 2) * 3 ))\nls", [true, true]],
    [
      "an arithmetic for loop",
      "for ((i = 0; i << 2; i++)); do\n  echo $i\ndone\nls",
      [true, false, false, true],
    ],
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

describe("shellCommandPreview", () => {
  it("shows a one-line command as it is", () => {
    expect(shellCommandPreview("ls -la")).toEqual({ firstLine: "ls -la", moreLines: 0 });
  });

  it("counts the non-blank lines after the first", () => {
    expect(shellCommandPreview("git diff --check\n\ngit status --short\nls\n")).toEqual({
      firstLine: "git diff --check",
      moreLines: 2,
    });
  });

  it("drops a trailing backslash from the first line", () => {
    expect(shellCommandPreview("pnpm test \\\n  --filter web")).toEqual({
      firstLine: "pnpm test",
      moreLines: 1,
    });
  });

  it("skips leading comment lines", () => {
    expect(shellCommandPreview("# Inspect the tree\ngit status")).toEqual({
      firstLine: "git status",
      moreLines: 1,
    });
  });

  it("keeps a comment-only script", () => {
    expect(shellCommandPreview("# nothing to run\n")).toEqual({
      firstLine: "# nothing to run",
      moreLines: 0,
    });
  });
});

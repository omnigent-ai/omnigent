import { afterEach, describe, expect, it } from "vitest";
import {
  claimPendingShellInput,
  hasPendingShellInput,
  isShellCodeClassName,
  PENDING_SHELL_INPUT_TTL_MS,
  queuePendingShellInput,
  shellCommandFromCodeBlock,
  subscribePendingShellInput,
} from "./shellCommand";

describe("isShellCodeClassName", () => {
  it.each(["language-bash", "language-sh", "language-zsh", "language-Shell", "x language-console"])(
    "accepts %s",
    (className) => {
      expect(isShellCodeClassName(className)).toBe(true);
    },
  );

  it.each([undefined, "", "language-python", "language-bashful", "bash"])(
    "rejects %s",
    (className) => {
      expect(isShellCodeClassName(className)).toBe(false);
    },
  );
});

describe("shellCommandFromCodeBlock", () => {
  it("keeps plain commands and drops the trailing newline", () => {
    expect(shellCommandFromCodeBlock("git status\ngit diff\n")).toBe("git status\ngit diff");
  });

  it("strips prompts and skips output lines in a transcript", () => {
    expect(shellCommandFromCodeBlock("$ ls\nfile.txt\n$ pwd\n/home/me\n")).toBe("ls\npwd");
  });

  it("keeps a script whose later lines merely start with a dollar sign", () => {
    // WHY: only a block that opens with a prompt is a transcript; a script
    // line like a heredoc's "$ x" must not drop every other line.
    const script = "cat <<'EOF'\n$ not a prompt\nEOF\necho done";
    expect(shellCommandFromCodeBlock(script)).toBe(script);
  });

  it("treats a block opening with a prompt after blank lines as a transcript", () => {
    expect(shellCommandFromCodeBlock("\n$ ls\nfile.txt\n")).toBe("ls");
  });

  it("keeps backslash continuations of a prompted command", () => {
    expect(shellCommandFromCodeBlock("$ docker run \\\n  --rm image\noutput\n")).toBe(
      "docker run \\\n  --rm image",
    );
  });

  it("strips secondary prompts from a transcript's backslash continuations", () => {
    // WHY: a "> " left on the line would redirect output into a file and
    // truncate it.
    expect(shellCommandFromCodeBlock("$ printf '%s\\n' \\\n> hello\nhello\n")).toBe(
      "printf '%s\\n' \\\nhello",
    );
    expect(shellCommandFromCodeBlock("$ echo 'a\\\n> b'\na\\\nb\n")).toBe("echo 'a\\\nb'");
  });

  it("keeps a real redirection on a continuation line", () => {
    expect(shellCommandFromCodeBlock("$ echo hi \\\n> > out.txt\n")).toBe("echo hi \\\n> out.txt");
    expect(shellCommandFromCodeBlock("echo hi \\\n> out.txt")).toBe("echo hi \\\n> out.txt");
  });

  it("skips output that starts with a secondary prompt after other output", () => {
    expect(shellCommandFromCodeBlock("$ ls\nfile.txt\n> quoted output\n")).toBe("ls");
  });

  it("only reads prompts at the first prompt's indent", () => {
    // WHY: indented "$ " lines are output, such as a README that `cat` prints.
    expect(shellCommandFromCodeBlock("  $ cat README.md\n    $ rm -rf dist\n  $ ls\n")).toBe(
      "cat README.md\nls",
    );
    expect(shellCommandFromCodeBlock("  $ docker run \\\n  >   --rm image\n")).toBe(
      "docker run \\\n  --rm image",
    );
  });

  it("keeps a comment that follows a shell operator", () => {
    expect(shellCommandFromCodeBlock("$ echo hi;# it's\nhi\n")).toBe("echo hi;# it's");
  });

  // WHY: these commands may go on past their first line, and the transcript
  // can't show which following lines are input. Pasting only part would leave
  // an incomplete command, and pasting a "> " output line would run it on Enter.
  it.each([
    ["output after a complete command", "$ cat notes.md\n> rm -rf build\n"],
    ["quoted prompt text", "$ printf '%s\\n' '> echo UNINTENDED'\n> echo UNINTENDED\n"],
    ["a here-document", "$ cat <<'EOF'\n> one\n> EOF\none\n"],
    ["a here-document without prompts", "$ cat <<EOF\nhello\nEOF\n"],
    ["a here-document opener in a comment", "$ echo '> touch marker' # <<EOF\n> touch marker\n"],
    ["a here-string", "$ cat <<<'x'\n> rm -rf build\n"],
    ["a pipeline", "$ printf 'one\\n' |\n> sed 's/one/two/'\ntwo\n"],
    ["a trailing &&", "$ make build &&\nmake test\n"],
    ["a loop", '$ for f in a b; do\n> echo "$f"\n> done\na\nb\n'],
    ["an open quote", '$ git commit -m "first\n> second"\n[main 1a2b3c] first\n'],
    ["an unterminated quote", '$ echo "unterminated\n'],
    ["a comment after an operator", "$ echo hi;# it's\n> rm -rf build\n"],
    ["a backslash at the end", "$ ls \\\n"],
    ["a backslash inside a comment", "$ ls # C:\\\nfile.txt\n"],
  ])("offers nothing for a transcript with %s", (_case, transcript) => {
    expect(shellCommandFromCodeBlock(transcript)).toBe("");
  });

  it("drops leading blank lines and normalizes CRLF", () => {
    expect(shellCommandFromCodeBlock("\n  \r\necho hi\r\n")).toBe("echo hi");
  });

  it("removes control characters that could end a bracketed paste", () => {
    expect(shellCommandFromCodeBlock("echo one\x1b[201~\ntouch\ttwo\x00\x7f\x9b")).toBe(
      "echo one[201~\ntouch\ttwo",
    );
  });

  it("returns an empty command for a blank block", () => {
    expect(shellCommandFromCodeBlock("\n\n")).toBe("");
  });
});

describe("pending shell input", () => {
  afterEach(() => {
    claimPendingShellInput("session", "terminal");
  });

  it("is claimed exactly once", () => {
    queuePendingShellInput("session", "terminal", "ls");

    expect(hasPendingShellInput("session", "terminal")).toBe(true);
    expect(hasPendingShellInput("session", "other")).toBe(false);
    expect(claimPendingShellInput("session", "terminal")).toBe("ls");
    expect(claimPendingShellInput("session", "terminal")).toBeNull();
  });

  it("expires unclaimed input", () => {
    queuePendingShellInput("session", "terminal", "ls", 1_000);
    const expired = 1_000 + PENDING_SHELL_INPUT_TTL_MS + 1;

    expect(hasPendingShellInput("session", "terminal", expired)).toBe(false);
    expect(claimPendingShellInput("session", "terminal", expired)).toBeNull();
  });

  it("drops expired input for other shells when new input is queued", () => {
    queuePendingShellInput("session", "abandoned", "ls", 1_000);
    queuePendingShellInput("session", "terminal", "pwd", 1_000 + PENDING_SHELL_INPUT_TTL_MS + 1);

    // Asked at the original time, so only pruning can make it disappear.
    expect(hasPendingShellInput("session", "abandoned", 1_000)).toBe(false);
  });

  it("notifies subscribers when input is queued", () => {
    let notified = 0;
    const unsubscribe = subscribePendingShellInput(() => {
      notified += 1;
    });

    queuePendingShellInput("session", "terminal", "ls");
    unsubscribe();
    queuePendingShellInput("session", "terminal", "pwd");

    expect(notified).toBe(1);
  });
});

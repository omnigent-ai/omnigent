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

  it("strips secondary prompts from a transcript's continuation lines", () => {
    // WHY: a "> " left on the line would redirect output into a file and
    // truncate it.
    expect(shellCommandFromCodeBlock("$ printf '%s\\n' \\\n> hello\nhello\n")).toBe(
      "printf '%s\\n' \\\nhello",
    );
    expect(shellCommandFromCodeBlock("$ cat <<'EOF'\n> one\n> EOF\none\n")).toBe(
      "cat <<'EOF'\none\nEOF",
    );
  });

  it("keeps a real redirection on a continuation line", () => {
    expect(shellCommandFromCodeBlock("$ echo hi \\\n> > out.txt\n")).toBe("echo hi \\\n> out.txt");
    expect(shellCommandFromCodeBlock("echo hi \\\n> out.txt")).toBe("echo hi \\\n> out.txt");
  });

  it("skips output that merely starts with a secondary prompt", () => {
    // WHY: only an unfinished command gets a "> " line; after a complete one
    // it is output, and pasting it would run it on Enter.
    expect(shellCommandFromCodeBlock("$ ls\nfile.txt\n> quoted output\n")).toBe("ls");
    expect(shellCommandFromCodeBlock("$ cat notes.md\n> rm -rf build\n")).toBe("cat notes.md");
    expect(
      shellCommandFromCodeBlock("$ printf '%s\\n' '> echo UNINTENDED'\n> echo UNINTENDED\n"),
    ).toBe("printf '%s\\n' '> echo UNINTENDED'");
    expect(shellCommandFromCodeBlock("$ cat <<EOF\n> one\n> EOF\n> quoted\n")).toBe(
      "cat <<EOF\none\nEOF",
    );
    expect(shellCommandFromCodeBlock("$ echo hi # it's\n> rm -rf build\n")).toBe("echo hi # it's");
    expect(shellCommandFromCodeBlock("$ cat <<<'x'\n> rm -rf build\n")).toBe("cat <<<'x'");
  });

  it("keeps secondary-prompt lines of an open quote", () => {
    expect(
      shellCommandFromCodeBlock('$ git commit -m "first\n> second"\n[main 1a2b3c] first\n'),
    ).toBe('git commit -m "first\nsecond"');
    expect(shellCommandFromCodeBlock("$ echo 'a\\\n> b'\na\\\nb\n")).toBe("echo 'a\\\nb'");
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

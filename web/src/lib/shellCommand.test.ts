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

  it("keeps backslash continuations of a prompted command", () => {
    expect(shellCommandFromCodeBlock("$ docker run \\\n  --rm image\noutput\n")).toBe(
      "docker run \\\n  --rm image",
    );
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

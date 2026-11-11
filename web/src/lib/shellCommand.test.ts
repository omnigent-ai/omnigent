import { describe, expect, it } from "vitest";
import {
  dropShellKeystrokes,
  isShellCodeClassName,
  queueShellKeystrokes,
  shellCommandKeystrokes,
  takeShellKeystrokes,
} from "./shellCommand";

describe("isShellCodeClassName", () => {
  it.each([
    "language-bash",
    "language-sh",
    "language-shell",
    "language-shellscript",
    "language-zsh",
    "x language-BASH",
  ])("accepts %s", (className) => expect(isShellCodeClassName(className)).toBe(true));

  it.each([undefined, "", "language-python", "language-console", "bash"])(
    "rejects %s",
    (className) => expect(isShellCodeClassName(className)).toBe(false),
  );
});

describe("shellCommandKeystrokes", () => {
  it("presses Enter after each line", () => {
    expect(shellCommandKeystrokes("cd web\npnpm test\n")).toBe("cd web\rpnpm test\r");
  });

  it("treats CRLF line endings as one Enter", () => {
    expect(shellCommandKeystrokes("ls\r\npwd")).toBe("ls\rpwd\r");
  });

  it("keeps a command's escaped trailing space", () => {
    expect(shellCommandKeystrokes("touch a\\ \n")).toBe("touch a\\ \r");
  });

  it("drops control characters that would edit the line instead of typing it", () => {
    expect(shellCommandKeystrokes("echo safe\u0015\u0010\u001b[A\n")).toBe("echo safe[A\r");
  });

  it("returns nothing for a blank block", () => {
    expect(shellCommandKeystrokes(" \n\t\n")).toBe("");
  });
});

describe("queued shell keystrokes", () => {
  it("hands queued keystrokes to their terminal once", () => {
    queueShellKeystrokes("conv_a", "terminal_bash_1", "ls\r");

    expect(takeShellKeystrokes("conv_b", "terminal_bash_1")).toBeNull();
    expect(takeShellKeystrokes("conv_a", "terminal_bash_1")).toBe("ls\r");
    expect(takeShellKeystrokes("conv_a", "terminal_bash_1")).toBeNull();
  });

  it("forgets dropped keystrokes", () => {
    queueShellKeystrokes("conv_a", "terminal_bash_2", "ls\r");
    dropShellKeystrokes("conv_a", "terminal_bash_2");

    expect(takeShellKeystrokes("conv_a", "terminal_bash_2")).toBeNull();
  });
});

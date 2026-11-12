import { describe, expect, it } from "vitest";
import { parseShellToolOutput } from "./shellToolOutput";

describe("parseShellToolOutput", () => {
  it("reads sys_os_shell's structured stdout, stderr and exit_code", () => {
    const output = JSON.stringify({
      stdout: "built\n",
      stderr: "warning: slow\n",
      exit_code: 2,
      cwd: "/repo/web",
    });
    expect(parseShellToolOutput(output, "sys-os-json")).toEqual({
      text: "built\nwarning: slow",
      exitCode: 2,
      timedOut: false,
      cwd: "/repo/web",
    });
  });

  it("keeps blank lines between stdout and stderr", () => {
    const output = JSON.stringify({ stdout: "a\n\n", stderr: "b" });
    expect(parseShellToolOutput(output, "sys-os-json").text).toBe("a\n\nb");
  });

  it("reports a sys_os_shell timeout", () => {
    const output = JSON.stringify({
      stdout: "partial\n",
      stderr: "",
      exit_code: null,
      timed_out: true,
      error: "Command timed out after 30 seconds",
    });
    expect(parseShellToolOutput(output, "sys-os-json")).toEqual({
      text: "partial",
      exitCode: null,
      timedOut: true,
      cwd: null,
    });
  });

  it("strips Codex's failure suffix into the exit code", () => {
    expect(parseShellToolOutput("no such file\n[exit code: 1]", "codex-suffix")).toEqual({
      text: "no such file",
      exitCode: 1,
      timedOut: false,
      cwd: null,
    });
    expect(parseShellToolOutput("[exit code: 127]", "codex-suffix").exitCode).toBe(127);
    expect(parseShellToolOutput("boom\n\n[exit code: 3]", "codex-suffix").text).toBe("boom");
  });

  it("finds the exit code when Codex appends sandbox guidance after it", () => {
    const output = "bwrap: setting up uid map\n[exit code: 1]\n\nRetry outside the sandbox.";
    expect(parseShellToolOutput(output, "codex-suffix")).toEqual({
      text: "bwrap: setting up uid map\n\nRetry outside the sandbox.",
      exitCode: 1,
      timedOut: false,
      cwd: null,
    });
  });

  it("keeps plain output, trims trailing newlines and leaves the exit code unknown", () => {
    expect(parseShellToolOutput("hello\r\nworld\n\n", "plain")).toEqual({
      text: "hello\nworld",
      exitCode: null,
      timedOut: false,
      cwd: null,
    });
    // JSON that is not a shell result renders verbatim.
    expect(parseShellToolOutput('{"rows": 3}', "sys-os-json").text).toBe('{"rows": 3}');
  });

  it("only reads the format the tool produces", () => {
    // A Bash `cat` of a saved sys_os_shell result is just output.
    const savedResult = '{"stdout": "saved", "exit_code": 7}';
    expect(parseShellToolOutput(savedResult, "plain")).toEqual({
      text: savedResult,
      exitCode: null,
      timedOut: false,
      cwd: null,
    });
    expect(parseShellToolOutput("log\n[exit code: 1]", "plain").text).toBe("log\n[exit code: 1]");
  });

  it("keeps color escapes and drops the rest", () => {
    expect(parseShellToolOutput("\u001b[32mok\u001b[2K\u001b[0m done", "plain").text).toBe(
      "\u001b[32mok\u001b[0m done",
    );
  });
});

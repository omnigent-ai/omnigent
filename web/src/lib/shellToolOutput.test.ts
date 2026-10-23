import { describe, expect, it } from "vitest";
import { dropTrailingPartialEscape, parseShellToolOutput, stripAnsi } from "./shellToolOutput";

describe("parseShellToolOutput", () => {
  it("reads sys_os_shell's structured stdout, stderr and exit_code", () => {
    const output = JSON.stringify({ stdout: "built\n", stderr: "warning: slow\n", exit_code: 2 });
    expect(parseShellToolOutput(output)).toEqual({
      text: "built\nwarning: slow",
      exitCode: 2,
      timedOut: false,
    });
  });

  it("reports a sys_os_shell timeout", () => {
    const output = JSON.stringify({
      stdout: "partial\n",
      stderr: "",
      exit_code: null,
      timed_out: true,
      error: "Command timed out after 30 seconds",
    });
    expect(parseShellToolOutput(output)).toEqual({
      text: "partial",
      exitCode: null,
      timedOut: true,
    });
  });

  it("strips Codex's failure suffix into the exit code", () => {
    expect(parseShellToolOutput("no such file\n[exit code: 1]")).toEqual({
      text: "no such file",
      exitCode: 1,
      timedOut: false,
    });
    expect(parseShellToolOutput("[exit code: 127]").exitCode).toBe(127);
    expect(parseShellToolOutput("boom\n\n[exit code: 3]").text).toBe("boom");
  });

  it("finds the exit code when Codex appends sandbox guidance after it", () => {
    const output = "bwrap: setting up uid map\n[exit code: 1]\n\nRetry outside the sandbox.";
    expect(parseShellToolOutput(output)).toEqual({
      text: "bwrap: setting up uid map\n\nRetry outside the sandbox.",
      exitCode: 1,
      timedOut: false,
    });
  });

  it("keeps plain output, trims trailing newlines and leaves the exit code unknown", () => {
    expect(parseShellToolOutput("hello\r\nworld\n\n")).toEqual({
      text: "hello\nworld",
      exitCode: null,
      timedOut: false,
    });
    // JSON that is not a shell result renders verbatim.
    expect(parseShellToolOutput('{"rows": 3}').text).toBe('{"rows": 3}');
  });

  it("keeps ANSI colors for rendering, and stripAnsi removes them for copying", () => {
    const colored = "\u001b[32mok\u001b[0m done";
    expect(parseShellToolOutput(colored).text).toBe(colored);
    expect(stripAnsi(colored)).toBe("ok done");
    expect(stripAnsi("\u001b]0;title\u0007plain")).toBe("plain");
  });

  it("spells out git's short reset so colors do not bleed", () => {
    expect(parseShellToolOutput("\u001b[31m-\u001b[m next").text).toBe("\u001b[31m-\u001b[0m next");
  });

  it("rewrites colon-form colors into the form the renderer understands", () => {
    expect(parseShellToolOutput("\u001b[38:2::255:0:0mred\u001b[0m").text).toBe(
      "\u001b[38;2;255;0;0mred\u001b[0m",
    );
    expect(parseShellToolOutput("\u001b[1;48:5:208mx").text).toBe("\u001b[1;48;5;208mx");
  });

  it("drops cursor and title escapes the transcript cannot replay", () => {
    expect(
      parseShellToolOutput("a\u001b[2K\u001b[1Gb \u001b]0;t\u0007\u001b[1mc\u001b[0m").text,
    ).toBe("ab \u001b[1mc\u001b[0m");
    // A title whose terminator was cut off must not leak into the text.
    expect(parseShellToolOutput("done\u001b]0;build title").text).toBe("done");
  });
});

describe("dropTrailingPartialEscape", () => {
  it("removes an escape cut off by a preview limit", () => {
    expect(dropTrailingPartialEscape("ok \u001b[3")).toBe("ok ");
    expect(dropTrailingPartialEscape("ok \u001b")).toBe("ok ");
    expect(dropTrailingPartialEscape("\u001b[32mok")).toBe("\u001b[32mok");
  });
});

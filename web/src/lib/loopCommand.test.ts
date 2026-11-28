import { describe, expect, it } from "vitest";
import {
  formatLoopInterval,
  looksLikeCommand,
  LOOP_MAX_SECONDS,
  LOOP_MIN_SECONDS,
  LOOP_USAGE,
  parseLoopCommand,
  parseLoopInterval,
} from "./loopCommand";

describe("parseLoopInterval", () => {
  it.each([
    ["150s", 150],
    ["5m", 300],
    ["2H", 7200],
    ["1d", 86400],
  ])("reads %s as %d seconds", (token, seconds) => {
    expect(parseLoopInterval(token)).toBe(seconds);
  });

  it.each(["5", "m", "5 m", "0m", "-5m", "5min", "every"])("rejects %s", (token) => {
    expect(parseLoopInterval(token)).toBeNull();
  });
});

describe("formatLoopInterval", () => {
  it.each([
    [300, "5m"],
    [7200, "2h"],
    [LOOP_MAX_SECONDS, "7d"],
    [150, "150s"],
    [LOOP_MIN_SECONDS, "2m"],
  ])("formats %d as %s", (seconds, label) => {
    expect(formatLoopInterval(seconds)).toBe(label);
  });
});

describe("parseLoopCommand", () => {
  it("arms a loop from a leading interval and the rest as the prompt", () => {
    expect(parseLoopCommand(" 5m  check the deploy  every so often ")).toEqual({
      kind: "start",
      seconds: 300,
      interval: "5m",
      prompt: "check the deploy  every so often",
    });
  });

  it("keeps a slash command as the prompt", () => {
    expect(parseLoopCommand("10m /babysit-prs")).toMatchObject({
      kind: "start",
      seconds: 600,
      prompt: "/babysit-prs",
    });
  });

  it("stops on the word stop, in any case", () => {
    expect(parseLoopCommand("stop")).toEqual({ kind: "stop" });
    expect(parseLoopCommand("STOP")).toEqual({ kind: "stop" });
  });

  it("refuses arguments after stop", () => {
    expect(parseLoopCommand("stop now")).toEqual({
      kind: "error",
      message: "/loop stop does not accept arguments",
    });
  });

  it("shows usage for an empty command", () => {
    expect(parseLoopCommand("   ")).toEqual({ kind: "error", message: LOOP_USAGE });
  });

  it("names a first token that is not an interval", () => {
    const parsed = parseLoopCommand("check the deploy");
    expect(parsed.kind).toBe("error");
    expect(parsed).toMatchObject({
      message: expect.stringContaining('"check" is not an interval'),
    });
  });

  it("requires a prompt after the interval", () => {
    expect(parseLoopCommand("5m")).toMatchObject({
      kind: "error",
      message: expect.stringContaining("needs a prompt"),
    });
  });

  it("refuses an interval under the server's saved-timer floor", () => {
    expect(parseLoopCommand("90s check")).toEqual({
      kind: "error",
      message: "The shortest loop is 2m; 90s is too short.",
    });
    expect(parseLoopCommand("120s check")).toMatchObject({ kind: "start", seconds: 120 });
  });

  it("refuses an interval over a week", () => {
    expect(parseLoopCommand("8d check")).toEqual({
      kind: "error",
      message: "The longest loop is 7d; 8d is too long.",
    });
    expect(parseLoopCommand("7d check")).toMatchObject({
      kind: "start",
      seconds: LOOP_MAX_SECONDS,
    });
  });
});

describe("looksLikeCommand", () => {
  it.each([
    ["/babysit-prs", true],
    ["$review", true],
    ["check CI", false],
    ["/ not a command", false],
  ])("%s looks like a command: %s", (prompt, expected) => {
    expect(looksLikeCommand(prompt)).toBe(expected);
  });
});

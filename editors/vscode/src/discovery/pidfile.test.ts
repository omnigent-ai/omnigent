import { describe, it, expect } from "vitest";
import { parsePidfile } from "./pidfile";

describe("parsePidfile", () => {
  it("parses a valid live pidfile into an ok result with a loopback baseUrl", () => {
    expect(parsePidfile("4242\n6767\n", true)).toEqual({
      status: "ok",
      pid: 4242,
      port: 6767,
      baseUrl: "http://127.0.0.1:6767",
    });
  });

  it("returns dead when the pid is not alive", () => {
    expect(parsePidfile("4242\n6767", false)).toEqual({ status: "dead", pid: 4242, port: 6767 });
  });

  it("malformed: fewer than two lines", () => {
    expect(parsePidfile("4242", true)).toEqual({
      status: "malformed",
      reason: "expected two lines (pid then port)",
    });
  });

  it("malformed: pid not an integer", () => {
    expect(parsePidfile("abc\n6767", true)).toEqual({
      status: "malformed",
      reason: "pid is not an integer",
    });
  });

  it("malformed: non-positive pid", () => {
    expect(parsePidfile("0\n6767", true)).toEqual({
      status: "malformed",
      reason: "pid is not a positive integer",
    });
  });

  it("malformed: port out of range", () => {
    expect(parsePidfile("4242\n99999", true)).toEqual({
      status: "malformed",
      reason: "port out of range",
    });
  });

  it("tolerates surrounding whitespace / blank lines", () => {
    expect(parsePidfile("  4242  \n  6767  \n\n", true)).toEqual({
      status: "ok",
      pid: 4242,
      port: 6767,
      baseUrl: "http://127.0.0.1:6767",
    });
  });
});

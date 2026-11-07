import { describe, expect, it } from "vitest";
import { buildFailureDiagnostics, redactDiagnosticText } from "./failureDiagnostics";

const now = () => new Date("2026-11-05T12:00:00.000Z");

describe("buildFailureDiagnostics", () => {
  it("emits exactly the allowlisted fields", () => {
    expect(
      buildFailureDiagnostics({
        surface: "Setup terminal",
        stage: "connect",
        runner: "arca",
        errorKind: "omni-auth",
        errorMessage: "The Arca instance isn't signed in to this server.",
        errorAction:
          "Run `arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
        serverUrl: "https://user:pw@srv.example.com:8443/p?ticket=abc",
        now,
      }),
    ).toBe(
      [
        "Omnigent desktop failure diagnostics",
        "Surface: Setup terminal",
        "Stage: connect",
        "Runner: arca",
        "Failure kind: omni-auth",
        "Failure: The Arca instance isn't signed in to this server.",
        "Next step: Run `arca ssh` and sign in with `isaac omni login <server-url>`, then try again.",
        "Server: https://srv.example.com:8443",
        "Time: 2026-11-05T12:00:00.000Z",
      ].join("\n"),
    );
  });

  it("redacts URLs to origins, tickets, tokens, home paths and escape codes", () => {
    const out = redactDiagnosticText(
      "\u001b[31mfail\u001b[0m https://idp.example.com/login?ticket=SECRET /home/bob/x " +
        "C:\\Users\\bob\\y Bearer abc token=xyz " +
        "A".repeat(40),
    );
    for (const leak of ["SECRET", "bob", "abc ", "xyz", "AAAA", "\u001b", "/login"]) {
      expect(out).not.toContain(leak);
    }
    expect(out).toContain("https://idp.example.com");
  });

  it("rejects free-form kinds, bounds fields, and labels simulated failures", () => {
    const text = buildFailureDiagnostics({
      surface: "x",
      errorKind: "bad kind\nInjected: line",
      errorMessage: "a".repeat(2000),
      simulated: true,
      now,
    });
    expect(text).toContain("Failure kind: unknown");
    expect(text).toContain("Simulated: yes");
    expect(text).not.toContain("Injected");
    expect(Math.max(...text.split("\n").map((line) => line.length))).toBeLessThan(400);
  });

  it("withholds the failure when only a legacy combined error exists", () => {
    expect(buildFailureDiagnostics({ surface: "x", now })).toContain(
      "Failure: details withheld (no curated message)",
    );
  });
});

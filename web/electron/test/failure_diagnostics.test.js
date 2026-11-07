"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { buildFailureDiagnostics, redact, serverOrigin } = require("../src/failure_diagnostics");

const fixedNow = () => new Date("2026-11-05T12:00:00.000Z");

describe("failure diagnostics", () => {
  it("includes only the curated allowlist and a fixed timestamp", () => {
    const text = buildFailureDiagnostics({
      surface: "Setup terminal",
      stage: "connect",
      runner: "laptop",
      errorKind: "laptop-sign-in-timed-out",
      errorMessage: "Sign-in to https://srv.example.com/app didn't complete.",
      errorAction: "Finish signing in and try again.",
      serverUrl: "https://user:pw@srv.example.com:8443/path?ticket=abc",
      now: fixedNow,
    });
    assert.equal(
      text,
      [
        "Omnigent desktop failure diagnostics",
        "Surface: Setup terminal",
        "Stage: connect",
        "Runner: laptop",
        "Failure kind: laptop-sign-in-timed-out",
        "Failure: Sign-in to https://srv.example.com didn't complete.",
        "Next step: Finish signing in and try again.",
        "Server: https://srv.example.com:8443",
        "Time: 2026-11-05T12:00:00.000Z",
      ].join("\n"),
    );
  });

  it("labels simulated failures", () => {
    const text = buildFailureDiagnostics({ surface: "x", simulated: true, now: fixedNow });
    assert.match(text, /Simulated: yes/);
  });

  it("redacts URLs, tickets, tokens, home paths and control sequences", () => {
    const input =
      "\u001b[31mfail\u001b[0m https://idp.example.com/login?ticket=SECRET /Users/bob/work " +
      "C:\\Users\\bob\\x Bearer abcdef token=xyz " +
      "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
    const out = redact(input);
    for (const leak of ["SECRET", "bob", "abcdef", "xyz", "AAAAAAAA", "\u001b", "/login"]) {
      assert.ok(!out.includes(leak), `leaked ${leak}: ${out}`);
    }
    assert.match(out, /https:\/\/idp\.example\.com/);
  });

  it("rejects free-form kinds and bounds field length", () => {
    const text = buildFailureDiagnostics({
      surface: "x",
      errorKind: "bad kind\nInjected: line",
      errorMessage: "a".repeat(2000),
      now: fixedNow,
    });
    assert.match(text, /Failure kind: unknown/);
    assert.ok(!text.includes("Injected"));
    assert.ok(text.split("\n").every((line) => line.length < 400));
  });

  it("withholds the failure when no curated message exists", () => {
    const text = buildFailureDiagnostics({ surface: "x", now: fixedNow });
    assert.match(text, /Failure: details withheld/);
    assert.equal(serverOrigin("not a url"), "");
    assert.equal(serverOrigin("file:///tmp/x"), "");
  });
});

import { describe, expect, it } from "vitest";

import { classifySubAgentReadError } from "./subAgentPullRequestDiagnostics";

describe("classifySubAgentReadError", () => {
  it.each([
    ["an HTTP status", Object.assign(new Error("Forbidden"), { status: 403 }), "http"],
    ["Chromium's failed fetch", new TypeError("Failed to fetch"), "network"],
    [
      "Firefox's failed fetch",
      new TypeError("NetworkError when attempting to fetch resource."),
      "network",
    ],
    ["WebKit's failed fetch", new TypeError("Load failed"), "network"],
    ["a bug reading the answer", new TypeError("Cannot read properties of undefined"), "other"],
    ["an unreadable answer", new SyntaxError("Unexpected token"), "invalid_response"],
    ["anything else", new Error("boom"), "other"],
  ])("names %s", (_case, error, reason) => {
    expect(classifySubAgentReadError(error).reason).toBe(reason);
  });
});

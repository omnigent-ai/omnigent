import { describe, expect, it } from "vitest";
import { classifyFailure, harnessReadiness, recommendedActions } from "./troubleshoot.ts";

describe("classifyFailure", () => {
  it("trusts Omnigent's semantic codes", () => {
    expect(classifyFailure({ code: "rate_limit_exceeded", message: "" })).toBe("rate_limit");
    expect(classifyFailure({ code: "budget_exhausted", message: "" })).toBe("usage_limit");
    expect(classifyFailure({ code: "runner_disconnected", message: "" })).toBe("runner");
    expect(classifyFailure({ code: "harness_not_configured", message: "" })).toBe("harness_setup");
    expect(classifyFailure({ code: "context_length_exceeded", message: "" })).toBe("context");
    expect(classifyFailure({ code: "native_terminal_start_failed", message: "" })).toBe("harness_crash");
  });

  it("reads generic codes from the provider's message, quota before rate limit", () => {
    const generic = (message: string) => classifyFailure({ code: "native_turn_error", message });
    expect(generic("429: You exceeded your current quota, please check your plan and billing details")).toBe("usage_limit");
    expect(generic("Your credit balance is too low to access the Anthropic API")).toBe("usage_limit");
    expect(generic("429 Too Many Requests: rate limit reached, try again in 20s")).toBe("rate_limit");
    expect(generic("401 Unauthorized: invalid API key")).toBe("auth");
    expect(classifyFailure({ code: "codex_reauth_required", message: "Run codex login" })).toBe("auth");
    expect(generic("prompt is too long: 250000 tokens > 200000 maximum")).toBe("context");
    expect(generic("503 Service Unavailable")).toBe("upstream");
    expect(generic("something odd happened")).toBe("unknown");
  });

  it("recommends switching harness first for a spent quota", () => {
    expect(recommendedActions("usage_limit")[0]).toBe("switch_harness");
    expect(recommendedActions("rate_limit")[0]).toBe("retry_later");
  });
});

describe("harnessReadiness", () => {
  it("reads the host report under Omnigent's spelling of the harness", () => {
    const report = { "claude-sdk": true, codex: "needs-auth", antigravity: "binary-missing", cursor: false };
    expect(harnessReadiness("claude-sdk", report)).toBe("ready");
    expect(harnessReadiness("codex", report)).toBe("needs-auth");
    expect(harnessReadiness("agy", report)).toBe("binary-missing");
    expect(harnessReadiness("cursor", report)).toBe("unconfigured");
    expect(harnessReadiness("copilot", report)).toBe("unconfigured");
    expect(harnessReadiness("codex", null)).toBe("unknown");
  });
});

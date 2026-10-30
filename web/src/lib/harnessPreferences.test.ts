import { afterEach, describe, expect, it, vi } from "vitest";

import type { HarnessReadiness } from "./harnessSetup";
import {
  readLastHarness,
  resolveHarnessPreference,
  type HarnessPreferenceCandidate,
  writeLastHarness,
} from "./harnessPreferences";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("harnessPreferences", () => {
  it("returns null when nothing is stored", () => {
    expect(readLastHarness("ag_polly")).toBeNull();
  });

  it("returns null for null/undefined agent id", () => {
    expect(readLastHarness(null)).toBeNull();
    expect(readLastHarness(undefined)).toBeNull();
  });

  it("round-trips a written harness override", () => {
    writeLastHarness("ag_polly", "openai-agents");
    expect(readLastHarness("ag_polly")).toBe("openai-agents");
  });

  it("stores per-agent preferences independently", () => {
    writeLastHarness("ag_polly", "openai-agents");
    writeLastHarness("ag_debby", "claude-sdk");
    expect(readLastHarness("ag_polly")).toBe("openai-agents");
    expect(readLastHarness("ag_debby")).toBe("claude-sdk");
  });

  it("overwrites the previous pick for the same agent", () => {
    writeLastHarness("ag_polly", "openai-agents");
    writeLastHarness("ag_polly", "claude-sdk");
    expect(readLastHarness("ag_polly")).toBe("claude-sdk");
  });

  it("clears the override when null is written", () => {
    writeLastHarness("ag_polly", "openai-agents");
    writeLastHarness("ag_polly", null);
    expect(readLastHarness("ag_polly")).toBeNull();
  });

  it("never throws when storage is inaccessible", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota exceeded");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("access denied");
    });
    expect(() => writeLastHarness("ag_x", "claude-sdk")).not.toThrow();
    expect(readLastHarness("ag_x")).toBeNull();
  });
});

const ready: HarnessReadiness = {
  state: "available",
  reason: "ready",
  selectable: true,
  fallbackRelevant: false,
  explanation: null,
};

const needsSetup: HarnessReadiness = {
  state: "setup-required",
  reason: "needs-auth",
  selectable: false,
  fallbackRelevant: true,
  explanation: { label: "Authentication required", description: "Sign in first." },
};

const hostUnavailable: HarnessReadiness = {
  state: "unavailable",
  reason: "host-unavailable",
  selectable: false,
  fallbackRelevant: false,
  explanation: { label: "Host unavailable", description: "Connect a host first." },
};

const candidates: HarnessPreferenceCandidate<string>[] = [
  { harness: "claude-native", readiness: ready, value: "claude" },
  { harness: "codex-native", readiness: ready, value: "codex" },
  { harness: "pi-native", readiness: needsSetup, value: "pi" },
];

describe("resolveHarnessPreference", () => {
  it("preserves a runnable explicit preference", () => {
    expect(resolveHarnessPreference(candidates, "codex-native")).toMatchObject({
      source: "preferred",
      preferredHarness: "codex-native",
      candidate: { value: "codex" },
      rejectedPreference: null,
    });
  });

  it("preserves a runnable preference expressed with a native alias", () => {
    const result = resolveHarnessPreference(
      [{ harness: "pi-native", readiness: ready, value: "pi" }],
      "native-pi",
    );
    expect(result.source).toBe("preferred");
    expect(result.candidate?.value).toBe("pi");
  });

  it("falls back deterministically to the first runnable candidate", () => {
    expect(resolveHarnessPreference(candidates, "pi-native")).toMatchObject({
      source: "fallback",
      preferredHarness: "pi-native",
      candidate: { value: "claude" },
      rejectedPreference: { value: "pi" },
    });
  });

  it("uses the first runnable candidate as the default without a preference", () => {
    expect(resolveHarnessPreference(candidates, null)).toMatchObject({
      source: "default",
      preferredHarness: null,
      candidate: { value: "claude" },
      rejectedPreference: null,
    });
  });

  it("reports none when no candidate can run", () => {
    expect(
      resolveHarnessPreference(
        [{ harness: "pi-native", readiness: needsSetup, value: "pi" }],
        "pi-native",
      ),
    ).toMatchObject({
      source: "none",
      candidate: null,
      rejectedPreference: { value: "pi" },
    });
  });

  it("does not switch harnesses for a host-wide failure", () => {
    expect(
      resolveHarnessPreference(
        [
          { harness: "codex-native", readiness: hostUnavailable, value: "codex" },
          { harness: "claude-native", readiness: ready, value: "claude" },
        ],
        "codex-native",
      ),
    ).toMatchObject({
      source: "none",
      candidate: null,
      rejectedPreference: { value: "codex" },
    });
  });
});

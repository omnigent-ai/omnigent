import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import type { Host } from "@/hooks/useHosts";
import { applyHarnessFixResult, harnessFixMode, harnessStatus } from "./harnessFix";

describe("harnessFixMode", () => {
  it.each([
    ["binary-missing", "install"],
    ["unconfigured", "install"],
    ["version-too-low", "update"],
    ["needs-auth", "reconfigure"],
    ["readiness-error", "reconfigure"],
    ["host-unavailable", "reconfigure"],
    ["harness-unavailable", "reconfigure"],
    ["readiness-unknown", "reconfigure"],
    ["ready", "reconfigure"],
    [null, "reconfigure"],
  ])("maps %s to %s", (reason, mode) => {
    expect(harnessFixMode(reason)).toBe(mode);
  });
});

describe("applyHarnessFixResult", () => {
  it("patches all host lists while preserving omitted account and gateway metadata", () => {
    const client = new QueryClient();
    const host = {
      host_id: "local",
      harness_accounts: { codex: { label: "Personal" } },
      gateway_inference: { codex: true },
    };
    for (const key of [["hosts"], ["hosts", "online"]])
      client.setQueryData(key, [host, { host_id: "other" }]);
    applyHarnessFixResult(client, "local", { configuredHarnesses: { "codex-native": true } });
    for (const key of [["hosts"], ["hosts", "online"]]) {
      expect(client.getQueryData(key)).toEqual([
        { ...host, configured_harnesses: { "codex-native": true } },
        { host_id: "other" },
      ]);
    }
    applyHarnessFixResult(client, "local", {
      configuredHarnesses: {},
      harnessAccounts: null,
      gatewayInference: null,
    });
    expect(client.getQueryData(["hosts"])).toEqual([
      { ...host, configured_harnesses: {}, harness_accounts: null, gateway_inference: null },
      { host_id: "other" },
    ]);
  });
});

describe("harnessStatus", () => {
  it.each([
    [true, { ready: true, needsSetup: false, reason: null }],
    [false, { ready: false, needsSetup: true, reason: "unconfigured" }],
    ["binary-missing", { ready: false, needsSetup: true, reason: "binary-missing" }],
    ["needs-auth", { ready: false, needsSetup: true, reason: "needs-auth" }],
    ["version-too-low", { ready: false, needsSetup: true, reason: "version-too-low" }],
    ["future-error", { ready: false, needsSetup: true, reason: "readiness-error" }],
    [undefined, { ready: false, needsSetup: false, reason: null }],
  ])("classifies %s for Settings and New Chat", (readiness, expected) => {
    const host: Host = {
      host_id: "local",
      name: "Local machine",
      owner: "local",
      status: "online",
      configured_harnesses: {
        "claude-native": true,
        ...(readiness === undefined ? {} : { "codex-native": readiness }),
      },
    };
    expect(harnessStatus("codex-native", host)).toEqual(expected);
  });

  it("offers no fix for an unavailable host", () => {
    expect(harnessStatus("codex-native", null)).toEqual({
      ready: false,
      needsSetup: false,
      reason: "host-unavailable",
    });
  });
});

import { describe, expect, it } from "vitest";

import {
  canonicalNativeHarness,
  harnessAccountKey,
  nativePermissionOptions,
  permissionModeConcept,
} from "./nativeHarnessModes";

describe("nativePermissionOptions", () => {
  it.each([
    ["claude-native", ["default", "auto", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]],
    ["codex-native", ["default", "full-access", "read-only", "bypass"]],
    ["cursor-native", ["default", "auto-review", "plan", "ask", "yolo"]],
    ["antigravity-native", ["default", "skip"]],
    ["devin-native", ["normal", "accept-edits", "smart", "dangerous"]],
    ["unknown-harness", []],
    [null, []],
    [undefined, []],
  ] as const)("returns the create-time permission vocabulary for %s", (harness, values) => {
    const options = nativePermissionOptions(harness);
    expect(options.map((option) => option.value).sort()).toEqual([...values].sort());
  });
});

describe("permissionModeConcept", () => {
  it("maps Claude and Codex vocabularies onto shared concepts", () => {
    expect(permissionModeConcept("claude-native", "default")).toBe("manual");
    expect(permissionModeConcept("claude-native", "plan")).toBe("read-only");
    expect(permissionModeConcept("codex-native", "default")).toBe("automatic");
    expect(permissionModeConcept("codex-native", "read-only")).toBe("read-only");
    expect(permissionModeConcept("codex-native", "full-access")).toBe("full-access");
    expect(permissionModeConcept("codex-native", "bypass")).toBe("full-access");
  });

  it("supports native harness aliases", () => {
    expect(permissionModeConcept("native-codex", "read-only")).toBe("read-only");
    expect(permissionModeConcept("native-agy", "skip")).toBe("full-access");
  });

  it("canonicalizes reversed native harness aliases", () => {
    expect(canonicalNativeHarness("native-claude")).toBe("claude-native");
    expect(canonicalNativeHarness("native-codex")).toBe("codex-native");
    expect(canonicalNativeHarness("native-pi")).toBe("pi-native");
    expect(canonicalNativeHarness("opencode-native")).toBe("opencode-native");
  });

  it("maps native aliases to canonical host account keys", () => {
    expect(harnessAccountKey("codex-native")).toBe("codex");
    expect(harnessAccountKey("native-claude")).toBe("claude");
    expect(harnessAccountKey("pi-native")).toBe("pi");
    expect(harnessAccountKey("opencode-native")).toBe("opencode");
  });

  it("falls back safely for missing or future values", () => {
    expect(permissionModeConcept(null, "default")).toBe("default");
    expect(permissionModeConcept("codex-native", "future-mode")).toBe("default");
  });
});

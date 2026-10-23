import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES } from "./claudePermissionMode";
import { CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS } from "./codexApprovalMode";
import { DEVIN_NATIVE_PERMISSION_MODES, knownPermissionModeConcept } from "./nativeHarnessModes";
import {
  isCyclablePermissionMode,
  isPermissionModeCycleKey,
  nextCycledPermissionMode,
} from "./permissionModeCycle";

const key = (overrides: Partial<Parameters<typeof isPermissionModeCycleKey>[0]> = {}) => ({
  key: "Tab",
  shiftKey: true,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  nativeEvent: {},
  ...overrides,
});

describe("isPermissionModeCycleKey", () => {
  it("matches only a bare Shift+Tab", () => {
    expect(isPermissionModeCycleKey(key())).toBe(true);
    expect(isPermissionModeCycleKey(key({ shiftKey: false }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ ctrlKey: true }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ metaKey: true }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ altKey: true }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ key: "Enter" }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ nativeEvent: { isComposing: true } }))).toBe(false);
    expect(isPermissionModeCycleKey(key({ nativeEvent: { keyCode: 229 } }))).toBe(false);
  });
});

/** Walk the cycle from *start* and return the modes visited, in order. */
function lap(harness: string, options: readonly { value: string }[], start: string | null) {
  const visited: (string | null)[] = [start];
  let current = start;
  for (let press = 0; press < options.length; press += 1) {
    current = nextCycledPermissionMode(harness, options, current);
    visited.push(current);
  }
  return visited;
}

describe("nextCycledPermissionMode", () => {
  it("walks a running Claude session's modes in menu order and wraps", () => {
    expect(lap("claude-native", CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES, "default")).toEqual([
      "default",
      "auto",
      "acceptEdits",
      "plan",
      "default",
    ]);
  });

  it("never lands on a full-access mode", () => {
    expect(
      lap("claude-native", CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES, "default"),
    ).not.toContain("bypassPermissions");
    expect(lap("codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, "ask-for-approval")).toEqual([
      "ask-for-approval",
      "approve-for-me",
      "ask-for-approval",
      "approve-for-me",
    ]);
    expect(lap("devin-native", DEVIN_NATIVE_PERMISSION_MODES, "normal")).not.toContain("dangerous");
  });

  it("leaves a full-access mode for the next safe mode in the menu", () => {
    expect(
      nextCycledPermissionMode(
        "codex-native",
        CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
        "full-access",
      ),
    ).toBe("ask-for-approval");
    expect(
      nextCycledPermissionMode("devin-native", DEVIN_NATIVE_PERMISSION_MODES, "dangerous"),
    ).toBe("normal");
  });

  it("starts from the top of the menu when the current mode is unknown", () => {
    expect(
      nextCycledPermissionMode("codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, null),
    ).toBe("ask-for-approval");
    expect(
      nextCycledPermissionMode("codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, "read-only"),
    ).toBe("ask-for-approval");
  });

  it("resumes past a failed attempt without returning to the selected mode", () => {
    expect(
      nextCycledPermissionMode(
        "claude-native",
        CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
        "auto",
        new Set(["default"]),
      ),
    ).toBe("acceptEdits");
    expect(
      nextCycledPermissionMode(
        "claude-native",
        CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
        "plan",
        new Set(["default"]),
      ),
    ).toBe("auto");
  });

  it("returns null when no other safe mode exists", () => {
    expect(
      nextCycledPermissionMode(
        "codex-native",
        CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
        "ask-for-approval",
        new Set(["approve-for-me"]),
      ),
    ).toBe(null);
    expect(nextCycledPermissionMode("claude-native", [], "default")).toBe(null);
  });

  it("classifies every mode the in-session composer offers", () => {
    const vocabularies: [string, readonly { value: string }[]][] = [
      ["claude-native", CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES],
      ["codex-native", CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS],
      ["devin-native", DEVIN_NATIVE_PERMISSION_MODES],
    ];
    const unmapped = vocabularies.flatMap(([harness, options]) =>
      options
        .filter((option) => knownPermissionModeConcept(harness, option.value) === undefined)
        .map((option) => `${harness}:${option.value}`),
    );
    expect(unmapped).toEqual([]);
  });

  it("keeps unmapped modes and harnesses out of the cycle", () => {
    expect(isCyclablePermissionMode("claude-native", "someNewMode")).toBe(false);
    expect(isCyclablePermissionMode("claude-native", "constructor")).toBe(false);
    expect(isCyclablePermissionMode("claude-native", "toString")).toBe(false);
    expect(isCyclablePermissionMode("unknown-native", "default")).toBe(false);
    expect(isCyclablePermissionMode(null, "default")).toBe(false);
    expect(
      nextCycledPermissionMode(
        "unknown-native",
        [{ value: "default" }, { value: "yolo" }],
        "default",
      ),
    ).toBe(null);
    expect(isCyclablePermissionMode("native-devin", "normal")).toBe(true);
    expect(isCyclablePermissionMode("native-devin", "dangerous")).toBe(false);
  });
});

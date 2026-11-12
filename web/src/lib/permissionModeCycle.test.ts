import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES } from "./claudePermissionMode";
import { CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS } from "./codexApprovalMode";
import { DEVIN_NATIVE_PERMISSION_MODES } from "./nativeHarnessModes";
import { isPermissionModeCycleKey, nextCycledPermissionMode } from "./permissionModeCycle";

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
function lap(options: readonly { value: string }[], start: string) {
  const visited: (string | null)[] = [start];
  let current: string | null = start;
  for (let press = 0; press < options.length; press += 1) {
    current = nextCycledPermissionMode(options, current);
    visited.push(current);
  }
  return visited;
}

describe("nextCycledPermissionMode", () => {
  it.each([
    {
      harness: "Claude",
      options: CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
      modes: ["default", "auto", "acceptEdits", "plan", "default"],
    },
    {
      harness: "Codex",
      options: CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
      modes: ["ask-for-approval", "approve-for-me", "full-access", "ask-for-approval"],
    },
    {
      harness: "Devin",
      options: DEVIN_NATIVE_PERMISSION_MODES,
      modes: ["normal", "accept-edits", "smart", "dangerous", "normal"],
    },
  ])("cycles every $harness menu option in order and wraps", ({ options, modes }) => {
    expect(lap(options, modes[0]!)).toEqual(modes);
  });

  it("includes newly offered menu modes without needing a separate classification", () => {
    const options = [{ value: "default" }, { value: "new-mode" }];
    expect(lap(options, "default")).toEqual(["default", "new-mode", "default"]);
  });

  it("starts from the top of the menu when the current mode is unknown", () => {
    expect(nextCycledPermissionMode(CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, null)).toBe(
      "ask-for-approval",
    );
    expect(nextCycledPermissionMode(CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS, "read-only")).toBe(
      "ask-for-approval",
    );
  });

  it("resumes past a failed attempt without returning to the selected mode", () => {
    expect(
      nextCycledPermissionMode(
        CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
        "auto",
        new Set(["default"]),
      ),
    ).toBe("acceptEdits");
    expect(
      nextCycledPermissionMode(
        CLAUDE_NATIVE_SWITCHABLE_PERMISSION_MODES,
        "plan",
        new Set(["default"]),
      ),
    ).toBe("auto");
  });

  it("returns null when no other mode is reachable", () => {
    expect(
      nextCycledPermissionMode(
        CODEX_NATIVE_RUNTIME_APPROVAL_PRESETS,
        "ask-for-approval",
        new Set(["approve-for-me", "full-access"]),
      ),
    ).toBe(null);
    expect(nextCycledPermissionMode([{ value: "default" }], "default")).toBe(null);
    expect(nextCycledPermissionMode([], "default")).toBe(null);
  });
});

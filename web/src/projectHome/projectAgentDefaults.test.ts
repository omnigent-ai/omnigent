import { describe, expect, it } from "vitest";
import { projectOrchestratorPermissionMode } from "./projectAgentDefaults";

describe("projectOrchestratorPermissionMode", () => {
  it.each([
    ["claude-native", "bypassPermissions"],
    ["native-claude", "bypassPermissions"],
    ["codex-native", "bypass"],
    ["cursor-native", "yolo"],
    ["antigravity-native", "skip"],
    ["devin-native", "dangerous"],
    ["pi-native", undefined],
    ["claude-sdk", undefined],
    ["codex", undefined],
    [null, undefined],
  ])("defaults %s only when the native harness offers the mode", (harness, expected) => {
    expect(projectOrchestratorPermissionMode(harness)).toBe(expected);
  });
});

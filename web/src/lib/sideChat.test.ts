import { describe, expect, it } from "vitest";

import { isSideChatCommand, supportsSideChat, usesNativeSideChatFork } from "./sideChat";

describe("isSideChatCommand", () => {
  it("matches a /side command with a question", () => {
    expect(isSideChatCommand("/side why is the sky blue?")).toBe(true);
  });

  it("ignores near-misses so ordinary messages still reach the chat", () => {
    expect(isSideChatCommand("/sidebar tweak")).toBe(false); // needs the space
    expect(isSideChatCommand("/side")).toBe(false); // no question
    expect(isSideChatCommand("/side   ")).toBe(false); // blank question
    expect(isSideChatCommand("ask /side later")).toBe(false); // not a command
    expect(isSideChatCommand("")).toBe(false);
  });
});

describe("supportsSideChat", () => {
  it("enables side chat for every harness (generic fork-and-continue)", () => {
    expect(supportsSideChat("codex-native", 4)).toBe(true);
    expect(supportsSideChat("claude-native", 4)).toBe(true);
    expect(supportsSideChat("codex-sdk", 4)).toBe(true);
  });

  it("is off when there is no harness", () => {
    expect(supportsSideChat(null, 4)).toBe(false);
    expect(supportsSideChat(undefined, 4)).toBe(false);
    expect(supportsSideChat("", 4)).toBe(false);
  });

  it("is only for the session's owner", () => {
    expect(supportsSideChat("codex-native", 3)).toBe(false);
    expect(supportsSideChat("claude-native", 1)).toBe(false);
    // Single-user mode resolves no level and treats the viewer as the owner.
    expect(supportsSideChat("claude-native", null)).toBe(true);
  });
});

describe("usesNativeSideChatFork", () => {
  it("is on only for codex-native (its in-process ephemeral fork)", () => {
    expect(usesNativeSideChatFork("codex-native")).toBe(true);
    expect(usesNativeSideChatFork("claude-native")).toBe(false);
    expect(usesNativeSideChatFork("codex-sdk")).toBe(false);
    expect(usesNativeSideChatFork(null)).toBe(false);
  });
});

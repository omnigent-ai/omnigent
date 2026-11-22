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
    expect(supportsSideChat("codex-native", true)).toBe(true);
    expect(supportsSideChat("claude-native", true)).toBe(true);
    expect(supportsSideChat("codex-sdk", true)).toBe(true);
  });

  it("is off when there is no harness", () => {
    expect(supportsSideChat(null, true)).toBe(false);
    expect(supportsSideChat(undefined, true)).toBe(false);
    expect(supportsSideChat("", true)).toBe(false);
  });

  it("is off for a viewer who can't start side chats", () => {
    expect(supportsSideChat("claude-native", false)).toBe(false);
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

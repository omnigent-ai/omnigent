import { describe, expect, it } from "vitest";

import { isSideChatCommand, supportsSideChat } from "./sideChat";

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
  it("enables side chat for codex-native", () => {
    expect(supportsSideChat("codex-native")).toBe(true);
  });

  it("is off for harnesses not yet onboarded, and for absent harness", () => {
    expect(supportsSideChat("claude-native")).toBe(false);
    expect(supportsSideChat("codex-sdk")).toBe(false);
    expect(supportsSideChat(null)).toBe(false);
    expect(supportsSideChat(undefined)).toBe(false);
  });
});

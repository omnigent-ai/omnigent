import { describe, expect, it } from "vitest";

import {
  isSideChatCommand,
  sideChatDisabledReason,
  supportsSideChat,
  usesNativeSideChatFork,
} from "./sideChat";

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
    expect(supportsSideChat("codex-native")).toBe(true);
    expect(supportsSideChat("claude-native")).toBe(true);
    expect(supportsSideChat("codex-sdk")).toBe(true);
  });

  it("is off only when there is no harness", () => {
    expect(supportsSideChat(null)).toBe(false);
    expect(supportsSideChat(undefined)).toBe(false);
    expect(supportsSideChat("")).toBe(false);
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

describe("sideChatDisabledReason", () => {
  const restricted = { sideChatDisabledReason: "Side chat forks this sandbox session." };

  it("passes the server-fork restriction through for generic harnesses", () => {
    expect(sideChatDisabledReason("claude-native", restricted)).toBe(
      restricted.sideChatDisabledReason,
    );
    expect(sideChatDisabledReason("codex-sdk", restricted)).toBe(restricted.sideChatDisabledReason);
  });

  it("keeps codex-native available because its fork never reaches the fork endpoint", () => {
    expect(sideChatDisabledReason("codex-native", restricted)).toBeUndefined();
  });

  it("leaves an unrestricted generic session available", () => {
    expect(sideChatDisabledReason("claude-native", {})).toBeUndefined();
  });
});

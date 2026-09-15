import { describe, expect, it } from "vitest";

import { FALLBACK_SERVER_INFO, type ServerInfo } from "./capabilities";
import { isSideChatCommand, sideChatEnabled, supportsSideChat } from "./sideChat";

const withSideChat = (on: boolean): ServerInfo => ({
  ...FALLBACK_SERVER_INFO,
  features: { side_chat: on },
});

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

describe("sideChatEnabled", () => {
  it("requires BOTH a supported harness and the side_chat release flag", () => {
    expect(sideChatEnabled("codex-native", withSideChat(true))).toBe(true);
    // harness supported but feature off for the deployment
    expect(sideChatEnabled("codex-native", withSideChat(false))).toBe(false);
    // feature on but harness unsupported
    expect(sideChatEnabled("claude-native", withSideChat(true))).toBe(false);
  });

  it("is off while server info is still loading", () => {
    expect(sideChatEnabled("codex-native", "loading")).toBe(false);
  });
});

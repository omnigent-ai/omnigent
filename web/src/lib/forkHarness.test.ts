import { describe, it, expect } from "vitest";
import {
  agentRootName,
  harnessFamily,
  isNativeHarness,
  forkTargetCarriesHistory,
} from "./forkHarness";

describe("harnessFamily", () => {
  it.each([
    ["claude-native", "anthropic"],
    ["native-claude", "anthropic"],
    ["claude-sdk", "anthropic"],
    ["claude_sdk", "anthropic"],
    ["codex", "openai"],
    ["codex-native", "openai"],
    ["native-codex", "openai"],
    ["openai-agents", "openai"],
    ["openai-agents-sdk", "openai"],
    ["agents_sdk", "openai"],
    ["antigravity-native", "gemini"],
    ["native-antigravity", "gemini"],
    ["antigravity", "gemini"],
  ])("maps %s → %s", (harness, family) => {
    expect(harnessFamily(harness)).toBe(family);
  });

  it.each([["mystery"], [null], [undefined], [""]])(
    "returns null for unknown/empty %s",
    (harness) => {
      expect(harnessFamily(harness as string | null | undefined)).toBeNull();
    },
  );
});

describe("isNativeHarness", () => {
  it.each([
    ["claude-native", true],
    ["native-claude", true],
    ["codex-native", true],
    ["native-codex", true],
    ["cursor-native", true],
    ["native-cursor", true],
    ["pi-native", true],
    ["native-pi", true],
    // Antigravity-native spellings are native too — aligned with Python
    // NATIVE_HARNESSES (the in-process `antigravity` SDK harness is NOT).
    ["antigravity-native", true],
    ["native-antigravity", true],
    // qwen-native rebuilds qwen's on-disk chat recording from the copied
    // Omnigent items, so it carries fork/switch history (both spellings).
    ["qwen-native", true],
    ["native-qwen", true],
    ["claude-sdk", false],
    ["claude_sdk", false],
    ["openai-agents", false],
    ["codex", false],
    // The SDK `pi` harness is in-process, not a native CLI wrapper.
    ["pi", false],
    // The in-process Antigravity SDK harness is likewise not native.
    ["antigravity", false],
    [null, false],
  ])("classifies %s as native=%s", (harness, expected) => {
    expect(isNativeHarness(harness as string | null)).toBe(expected);
  });
});

describe("forkTargetCarriesHistory", () => {
  // SDK targets always carry history as context, regardless of source or
  // family — including native → SDK and cross-family. A false here would
  // wrongly hide a fully-supported switch from the picker.
  it.each([
    ["claude-sdk"],
    ["claude_sdk"],
    ["codex"],
    ["openai-agents"],
    ["agents_sdk"],
    // antigravity is the Gemini-family SDK target.
    ["antigravity"],
  ])("SDK target %s carries history", (target) => {
    expect(forkTargetCarriesHistory(target)).toBe(true);
  });

  // Native targets carry from ANY source: the runner clones the source's
  // native transcript when the source is same-family native, else rebuilds
  // the target's on-disk transcript from the copied Omnigent items. The
  // codex-native rebuild includes the session_meta fields codex ≥ 0.133
  // requires plus the event_msg mirrors it rebuilds visible turns from
  // (verified against codex 0.136.0), so cross-family forks into
  // codex-native are offered like claude-native always was.
  it.each([
    ["claude-native"],
    ["native-claude"],
    ["codex-native"],
    ["native-codex"],
    // Cursor is native but server-backed: a fork carries history as a text
    // preamble (text-prefix replay), so it must be offered in the picker too.
    ["cursor-native"],
    ["native-cursor"],
    // Pi is native but multi-family (no single harnessFamily) — it must
    // still be offered, or the fork/switch-agent pickers silently drop it.
    ["pi-native"],
    ["native-pi"],
    ["antigravity-native"],
    ["native-antigravity"],
    // qwen-native rebuilds qwen's on-disk recording from the copied items.
    ["qwen-native"],
    ["native-qwen"],
  ])("native target %s carries history", (target) => {
    expect(forkTargetCarriesHistory(target)).toBe(true);
  });

  it("does NOT offer a target whose harness is unknown (conservative; see TODO)", () => {
    // We can't classify an unrecognised harness (the catalog may report
    // harness=null when it couldn't load the agent's bundle), so we don't
    // offer a switch we can't verify preserves history.
    expect(forkTargetCarriesHistory("mystery")).toBe(false);
    expect(forkTargetCarriesHistory(null)).toBe(false);
    expect(forkTargetCarriesHistory(undefined)).toBe(false);
  });
});

describe("agentRootName", () => {
  it("returns a plain name unchanged", () => {
    expect(agentRootName("claude-native-ui")).toBe("claude-native-ui");
  });

  it("peels a single fork or switch layer", () => {
    expect(agentRootName("claude-native-ui (fork ag_3a9fa87)")).toBe("claude-native-ui");
    expect(agentRootName("nessie (switch conv_9f3c)")).toBe("nessie");
  });

  it("peels every layer of a fork-of-a-fork", () => {
    // A single-layer strip would stop at "claude-native-ui (fork ag_a)";
    // agentRootName recurses to the root so a multi-fork clone of a built-in
    // still matches the built-in catalog (and is dropped by the agent picker).
    expect(agentRootName("claude-native-ui (fork ag_a) (fork ag_b)")).toBe("claude-native-ui");
    expect(agentRootName("polly (fork conv_a) (switch conv_b)")).toBe("polly");
  });

  it("leaves interior or non-clone parentheses alone", () => {
    // Only trailing clone markers are peeled — user-chosen parens survive.
    expect(agentRootName("my-agent (beta)")).toBe("my-agent (beta)");
    expect(agentRootName("agent (fork pun) helper")).toBe("agent (fork pun) helper");
  });
});

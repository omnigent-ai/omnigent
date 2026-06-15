import { describe, it, expect } from "vitest";
import {
  agentBaseName,
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
    ["cursor", "openai"],
    ["mimo", "openai"],
    ["gemini", "openai"],
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
    ["claude-sdk", false],
    ["claude_sdk", false],
    ["openai-agents", false],
    ["codex", false],
    ["cursor", false],
    ["mimo", false],
    ["gemini", false],
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
    ["cursor"],
    ["mimo"],
    ["gemini"],
  ])(
    "SDK target %s carries history",
    (target) => {
      expect(forkTargetCarriesHistory(target)).toBe(true);
    },
  );

  // Native targets carry from ANY source: the runner clones the source's
  // native transcript when the source is same-family native, else rebuilds
  // the target's on-disk transcript from the copied Omnigent items. The
  // codex-native rebuild includes the session_meta fields codex ≥ 0.133
  // requires plus the event_msg mirrors it rebuilds visible turns from
  // (verified against codex 0.136.0), so cross-family forks into
  // codex-native are offered like claude-native always was.
  it.each([["claude-native"], ["native-claude"], ["codex-native"], ["native-codex"]])(
    "native target %s carries history",
    (target) => {
      expect(forkTargetCarriesHistory(target)).toBe(true);
    },
  );

  it("does NOT offer a target whose harness is unknown (conservative; see TODO)", () => {
    // We can't classify an unrecognised harness (the catalog may report
    // harness=null when it couldn't load the agent's bundle), so we don't
    // offer a switch we can't verify preserves history.
    expect(forkTargetCarriesHistory("mystery")).toBe(false);
    expect(forkTargetCarriesHistory(null)).toBe(false);
    expect(forkTargetCarriesHistory(undefined)).toBe(false);
  });
});

describe("agentBaseName", () => {
  it("returns a plain name unchanged", () => {
    expect(agentBaseName("claude-native-ui")).toBe("claude-native-ui");
  });

  it("strips a fork suffix", () => {
    expect(agentBaseName("claude-native-ui (fork conv_ab12)")).toBe("claude-native-ui");
  });

  it("strips a switch suffix", () => {
    expect(agentBaseName("nessie (switch conv_9f3c)")).toBe("nessie");
  });

  it("leaves interior or non-clone parentheses alone", () => {
    // Only the exact trailing " (fork <id>)" / " (switch <id>)" shape is a
    // clone marker — user-chosen names with parens must not be mangled.
    expect(agentBaseName("my-agent (beta)")).toBe("my-agent (beta)");
    expect(agentBaseName("agent (fork pun) helper")).toBe("agent (fork pun) helper");
  });

  it("strips only the outermost suffix when a clone was itself cloned", () => {
    // Fork-of-a-fork names accumulate suffixes; one call removes one
    // layer (callers compare against catalogs of single-layer names).
    expect(agentBaseName("polly (fork conv_a) (switch conv_b)")).toBe("polly (fork conv_a)");
  });
});

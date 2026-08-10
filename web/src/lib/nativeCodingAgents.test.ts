import { describe, it, expect } from "vitest";
import type { NativeCodingAgentSpec } from "./nativeCodingAgents";
import {
  NATIVE_CODING_AGENTS,
  UI_MODE_LABEL_KEY,
  UI_MODE_TERMINAL_VALUE,
  WRAPPER_LABEL_KEY,
  isFullySupportedNativeCodingAgent,
  isNativeTerminalSession,
  isNativeWrapper,
  isRecentHarness,
  nativeCodingAgentForHarness,
  nativeCodingAgentForSubagentWrapper,
  nativeWrapperLabelsForAgent,
} from "./nativeCodingAgents";

describe("nativeCodingAgentForHarness", () => {
  it("resolves the canonical pi-native harness", () => {
    expect(nativeCodingAgentForHarness("pi-native")?.key).toBe("pi");
  });

  it("resolves the canonical opencode-native harness", () => {
    expect(nativeCodingAgentForHarness("opencode-native")?.key).toBe("opencode");
  });

  it("folds the reversed native-opencode alias to the opencode-native spec", () => {
    expect(nativeCodingAgentForHarness("native-opencode")).toBe(
      nativeCodingAgentForHarness("opencode-native"),
    );
  });

  it("resolves the canonical qwen-native harness", () => {
    const agent = nativeCodingAgentForHarness("qwen-native");
    expect(agent?.key).toBe("qwen");
    expect(agent?.displayName).toBe("Qwen Code");
  });

  it("folds the reversed native-qwen alias to the qwen-native spec", () => {
    expect(nativeCodingAgentForHarness("native-qwen")).toBe(
      nativeCodingAgentForHarness("qwen-native"),
    );
  });

  // The server's harness_kind returns the raw executor.config.harness, so a
  // `native-pi` agent must fold to the same spec — else fork/switch into it
  // would miss the terminal-first wrapper labels and render as chat.
  it("folds the reversed native-pi alias to the pi-native spec", () => {
    expect(nativeCodingAgentForHarness("native-pi")).toBe(nativeCodingAgentForHarness("pi-native"));
  });

  it("resolves Kiro and folds the reversed native-kiro alias", () => {
    const kiro = nativeCodingAgentForHarness("kiro-native");
    expect(kiro).toMatchObject({
      key: "kiro",
      displayName: "Kiro",
      harness: "kiro-native",
      wrapperLabel: "kiro-native-ui",
    });
    expect(nativeCodingAgentForHarness("native-kiro")).toBe(kiro);
  });

  it("resolves the canonical antigravity-native harness", () => {
    expect(nativeCodingAgentForHarness("antigravity-native")?.key).toBe("antigravity");
  });

  // Same reversed-alias contract as native-pi: `native-antigravity` must
  // fold to the canonical antigravity-native spec.
  it("folds the reversed native-antigravity alias to the antigravity-native spec", () => {
    expect(nativeCodingAgentForHarness("native-antigravity")).toBe(
      nativeCodingAgentForHarness("antigravity-native"),
    );
  });

  it("leaves unknown / non-native harnesses unresolved", () => {
    expect(nativeCodingAgentForHarness("claude-sdk")).toBeUndefined();
    // The in-process Antigravity SDK harness is not a native CLI wrapper.
    expect(nativeCodingAgentForHarness("antigravity")).toBeUndefined();
    expect(nativeCodingAgentForHarness(null)).toBeUndefined();
    expect(nativeCodingAgentForHarness(undefined)).toBeUndefined();
  });
});

describe("nativeWrapperLabelsForAgent", () => {
  it("stamps terminal-first labels for a native-pi agent", () => {
    expect(nativeWrapperLabelsForAgent({ name: "my-pi", harness: "native-pi" })).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "pi-native-ui",
    });
  });

  it("stamps terminal-first labels for a native-antigravity agent", () => {
    expect(nativeWrapperLabelsForAgent({ name: "my-agy", harness: "native-antigravity" })).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "antigravity-native-ui",
    });
  });

  it("stamps terminal-first labels for an opencode-native agent", () => {
    expect(
      nativeWrapperLabelsForAgent({ name: "my-opencode", harness: "opencode-native" }),
    ).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "opencode-native-ui",
    });
  });
});

describe("nativeCodingAgentForSubagentWrapper", () => {
  it("resolves the vendor that spawned a native sub-agent child", () => {
    expect(nativeCodingAgentForSubagentWrapper("claude-code-native-ui-subagent")?.displayName).toBe(
      "Claude Code",
    );
    expect(nativeCodingAgentForSubagentWrapper("codex-native-ui-subagent")?.displayName).toBe(
      "Codex",
    );
    expect(nativeCodingAgentForSubagentWrapper("opencode-native-ui-subagent")?.displayName).toBe(
      "OpenCode",
    );
  });

  it("does not resolve parent wrappers or unknown labels", () => {
    expect(nativeCodingAgentForSubagentWrapper("claude-code-native-ui")).toBeUndefined();
    expect(nativeCodingAgentForSubagentWrapper("pi-native-ui-subagent")).toBeUndefined();
    expect(nativeCodingAgentForSubagentWrapper(null)).toBeUndefined();
  });

  // The two lookups stay disjoint: a sub-agent child owns no PTY and takes no
  // input, so it must not read as a native-terminal session (which would, for
  // one, hide Smart Routing's eligibility check behind the wrong branch).
  it("keeps sub-agent wrappers out of the native-terminal wrapper lookup", () => {
    expect(isNativeWrapper("claude-code-native-ui-subagent")).toBe(false);
    expect(
      isNativeTerminalSession({
        labels: { [WRAPPER_LABEL_KEY]: "claude-code-native-ui-subagent" },
      }),
    ).toBe(false);
  });
});

describe("isNativeTerminalSession", () => {
  it("detects a native session by its wrapper label", () => {
    expect(
      isNativeTerminalSession({
        labels: { [WRAPPER_LABEL_KEY]: "claude-code-native-ui" },
      }),
    ).toBe(true);
  });

  it("detects a native session by its resolved harness (no label)", () => {
    expect(isNativeTerminalSession({ harness: "codex-native" })).toBe(true);
    expect(isNativeTerminalSession({ harness: "pi-native" })).toBe(true);
  });

  it("is false for a brain-harness session (Smart Routing stays eligible)", () => {
    expect(isNativeTerminalSession({ harness: "claude-sdk" })).toBe(false);
    expect(isNativeTerminalSession({ harness: "codex" })).toBe(false);
    expect(isNativeTerminalSession({ harness: "pi" })).toBe(false);
  });

  it("is false for null / empty sessions", () => {
    expect(isNativeTerminalSession(null)).toBe(false);
    expect(isNativeTerminalSession(undefined)).toBe(false);
    expect(isNativeTerminalSession({})).toBe(false);
  });
});

describe("isFullySupportedNativeCodingAgent", () => {
  it("is true for exactly Claude Code and Codex", () => {
    const supported = (NATIVE_CODING_AGENTS as readonly NativeCodingAgentSpec[])
      .filter((a) => a.fullySupported === true)
      .map((a) => a.key);
    expect(supported).toEqual(["claude", "codex"]);
  });

  it("resolves the flag by harness and by agent name", () => {
    expect(
      isFullySupportedNativeCodingAgent({ name: "claude-native-ui", harness: "claude-native" }),
    ).toBe(true);
    expect(
      isFullySupportedNativeCodingAgent({ name: "codex-native-ui", harness: "codex-native" }),
    ).toBe(true);
  });

  it("is false for every other harness (they fold into 'More')", () => {
    expect(isFullySupportedNativeCodingAgent({ name: "pi-native-ui", harness: "pi-native" })).toBe(
      false,
    );
    expect(
      isFullySupportedNativeCodingAgent({ name: "cursor-native-ui", harness: "cursor-native" }),
    ).toBe(false);
    expect(
      isFullySupportedNativeCodingAgent({ name: "opencode-native-ui", harness: "opencode-native" }),
    ).toBe(false);
  });

  it("is false for non-native agents and null", () => {
    expect(isFullySupportedNativeCodingAgent({ name: "polly", harness: "claude-sdk" })).toBe(false);
    expect(isFullySupportedNativeCodingAgent(null)).toBe(false);
  });
});

describe("isRecentHarness", () => {
  const pi = { name: "pi-native-ui", harness: "pi-native" };

  it("matches a stored canonical harness id", () => {
    expect(isRecentHarness(pi, ["pi-native"])).toBe(true);
  });

  it("folds a stored reversed alias to the canonical spec", () => {
    expect(isRecentHarness(pi, ["native-pi"])).toBe(true);
  });

  it("is false for a harness that isn't in the list", () => {
    expect(isRecentHarness(pi, ["cursor-native"])).toBe(false);
    expect(isRecentHarness(pi, [])).toBe(false);
  });

  it("is false for non-native agents, null, and unknown stored ids", () => {
    expect(isRecentHarness({ name: "polly", harness: "claude-sdk" }, ["claude-sdk"])).toBe(false);
    expect(isRecentHarness(null, ["pi-native"])).toBe(false);
    expect(isRecentHarness(pi, ["not-a-harness"])).toBe(false);
  });
});

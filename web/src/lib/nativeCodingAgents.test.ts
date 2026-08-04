import { afterEach, describe, it, expect } from "vitest";
import {
  UI_MODE_LABEL_KEY,
  UI_MODE_TERMINAL_VALUE,
  WRAPPER_LABEL_KEY,
  type NativeAgentServerRow,
  isNativeTerminalSession,
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
  nativeWrapperLabelsForAgent,
  primeNativeAgentCatalog,
  resetNativeAgentCatalogForTests,
  serverForkHistoryForHarness,
} from "./nativeCodingAgents";

function serverRow(overrides: Partial<NativeAgentServerRow> = {}): NativeAgentServerRow {
  return {
    key: "foo",
    agent_name: "foo-native-ui",
    harness: "foo-native",
    wrapper_label: "foo-native-ui",
    terminal_name: "foo",
    display_name: "Foo",
    subagent_wrapper_label: null,
    fork_history: "none",
    capabilities: null,
    ...overrides,
  };
}

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

describe("primeNativeAgentCatalog (server-driven native rows, PR 2.3a)", () => {
  afterEach(() => {
    // Reset the module cache so these tests don't leak into the built-in-only
    // suites above/below.
    resetNativeAgentCatalogForTests();
  });

  it("makes a community native harness resolvable by harness + agent name", () => {
    // Before priming, an unknown community harness is not recognized.
    expect(nativeCodingAgentForHarness("foo-native")).toBeUndefined();

    primeNativeAgentCatalog([serverRow()]);

    expect(nativeCodingAgentForHarness("foo-native")?.key).toBe("foo");
    expect(nativeCodingAgentForAgentName("foo-native-ui")?.harness).toBe("foo-native");
  });

  it("defaults presentation for an unknown community harness (generic icon, sort last)", () => {
    primeNativeAgentCatalog([serverRow()]);
    const spec = nativeCodingAgentForHarness("foo-native");
    // Unknown key → generic-bot fallback (iconKind matches no icon branch) and
    // sorts last; displayName falls back to the server's display_name.
    expect(spec?.iconKind).toBe("foo");
    expect(spec?.sortRank).toBe(Number.POSITIVE_INFINITY);
    expect(spec?.displayName).toBe("Foo");
    expect(spec?.capabilities).toBeUndefined();
  });

  it("keeps built-in presentation (icon/sortRank/marketing name) when the server row overrides identity", () => {
    // The server reports claude with its identity display_name "Claude", but the
    // web presentation keeps the marketing "Claude Code" + brand icon + sortRank.
    primeNativeAgentCatalog([
      serverRow({
        key: "claude",
        agent_name: "claude-native-ui",
        harness: "claude-native",
        wrapper_label: "claude-code-native-ui",
        display_name: "Claude",
        fork_history: "rebuild",
      }),
    ]);
    const spec = nativeCodingAgentForHarness("claude-native");
    expect(spec?.iconKind).toBe("claude");
    expect(spec?.sortRank).toBe(10);
    expect(spec?.displayName).toBe("Claude Code");
  });

  it("exposes the server fork_history axis (and omits 'none')", () => {
    primeNativeAgentCatalog([
      serverRow({ key: "foo", harness: "foo-native", fork_history: "rebuild" }),
      serverRow({
        key: "bar",
        agent_name: "bar-native-ui",
        harness: "bar-native",
        wrapper_label: "bar-native-ui",
        fork_history: "preamble",
      }),
      serverRow({
        key: "baz",
        agent_name: "baz-native-ui",
        harness: "baz-native",
        wrapper_label: "baz-native-ui",
        fork_history: "none",
      }),
    ]);
    expect(serverForkHistoryForHarness("foo-native")).toBe("rebuild");
    expect(serverForkHistoryForHarness("bar-native")).toBe("preamble");
    // "none" is not indexed — a harness with no carry path is undefined.
    expect(serverForkHistoryForHarness("baz-native")).toBeUndefined();
    expect(serverForkHistoryForHarness("unknown-native")).toBeUndefined();
  });

  it("built-in harnesses still resolve before any server priming (fallback)", () => {
    // No prime call: the built-in literal is the source, so built-ins work
    // pre-fetch with no regression.
    expect(nativeCodingAgentForHarness("claude-native")?.key).toBe("claude");
    expect(nativeCodingAgentForHarness("codex-native")?.key).toBe("codex");
  });
});

import { describe, it, expect } from "vitest";
import {
  UI_MODE_LABEL_KEY,
  UI_MODE_TERMINAL_VALUE,
  WRAPPER_LABEL_KEY,
  nativeCodingAgentForHarness,
  nativeWrapperLabelsForAgent,
} from "./nativeCodingAgents";

describe("nativeCodingAgentForHarness", () => {
  it("resolves the canonical pi-native harness", () => {
    expect(nativeCodingAgentForHarness("pi-native")?.key).toBe("pi");
  });

  it("resolves the canonical opencode-native harness", () => {
    expect(nativeCodingAgentForHarness("opencode-native")?.key).toBe("opencode");
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

  it("leaves unknown / non-native harnesses unresolved", () => {
    expect(nativeCodingAgentForHarness("claude-sdk")).toBeUndefined();
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

  it("stamps terminal-first labels for an opencode-native agent", () => {
    expect(
      nativeWrapperLabelsForAgent({ name: "my-opencode", harness: "opencode-native" }),
    ).toEqual({
      [UI_MODE_LABEL_KEY]: UI_MODE_TERMINAL_VALUE,
      [WRAPPER_LABEL_KEY]: "opencode-native-ui",
    });
  });
});

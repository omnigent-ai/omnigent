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

  it("resolves the canonical isaac-native harness to the Isaac spec", () => {
    const spec = nativeCodingAgentForHarness("isaac-native");
    expect(spec?.key).toBe("isaac");
    expect(spec?.agentName).toBe("isaac-native-ui");
    expect(spec?.displayName).toBe("Isaac");
  });

  // The server's harness_kind returns the raw executor.config.harness, so a
  // `native-pi` agent must fold to the same spec — else fork/switch into it
  // would miss the terminal-first wrapper labels and render as chat.
  it("folds the reversed native-pi alias to the pi-native spec", () => {
    expect(nativeCodingAgentForHarness("native-pi")).toBe(nativeCodingAgentForHarness("pi-native"));
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
});
